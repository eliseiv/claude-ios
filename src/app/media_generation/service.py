"""Image/video generation use-cases over the fal.ai queue (ADR-060, media-generation/03).

Two operations, both owner-scoped:

* ``submit`` — resolve the public model id, price it server-side, debit the credits, enqueue the
  run upstream and persist the job. The debit and the insert share the request transaction, so a
  failed submit (upstream 5xx/timeout) rolls the debit back: a user is never charged for a run fal
  did not accept.
* ``get_job`` — return the job, polling fal only while it is non-terminal. A run that fails
  upstream refunds its credits once (idempotent by job id) — the user paid for an output they
  never got.

Generation is asynchronous by nature (Veo/Kling take minutes), so ``submit`` returns a job in
``queued`` state and the client polls. There is no webhook: a poll-based contract needs no public
callback surface and no signature scheme, and the iOS client is already polling-shaped.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app import instance_config
from app.chat.attachments import (
    _check_magic_bytes,
    _decode_base64,
    _decoded_len_from_base64,
)
from app.config import Settings
from app.errors import (
    ContentPolicyViolationError,
    JobNotTerminalError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamJobGoneError,
    ValidationFailedError,
)
from app.media_generation.catalog import (
    KIND_IMAGE,
    KIND_VIDEO,
    FalModel,
    FalVariant,
    build_fal_input,
    find_model,
    resolve_values,
)
from app.media_generation.cursor import MediaJobCursor
from app.media_generation.fal_client import (
    FAL_CANCELED,
    FAL_COMPLETED,
    FAL_FAILED,
    FalClient,
    upstream_status_of,
)
from app.media_generation.repository import (
    STATUS_COMPLETED,
    STATUS_QUEUED,
    TERMINAL_STATUSES,
    MediaJobsRepository,
)
from app.media_generation.signed_url import public_asset_url
from app.models import MediaJob
from app.moderation import ModerationService, ModerationVerdict, unchecked_verdict
from app.moderation.service import (
    STAGE_INPUT,
    STAGE_OUTPUT,
    STATUS_BLOCKED,
    SURFACE_MEDIA_RESULT,
    SURFACE_MEDIA_SUBMIT,
    SURFACE_MEDIA_UPLOAD,
)
from app.notifications.push_service import MediaPushService
from app.observability.logging import log_event
from app.observability.metrics import moderation_decisions_total
from app.pricing.provider_prices import media_cost_usd_of_run, round_usd
from app.request_logs.service import RequestLogWriter
from app.wallet.service import WalletService

logger = logging.getLogger(__name__)

# ADR-086 §5 (видео): omni-moderation не принимает видеофайл, поэтому пост-модерация результата
# видео выполняется ЧУЖИМ сигналом — терминальным отказом самого fal по контент-политике. Флага в
# ответе видео-моделей нет (output schema Kling/Veo — одно поле `video`), поэтому единственный
# наблюдаемый признак — текст ошибки. Список маркеров намеренно узкий: расширять его догадками
# значит помечать `blocked` обычные сбои провайдера.
_FAL_CONTENT_POLICY_MARKERS = (
    "content policy",
    "content_policy",
    "safety",
    "nsfw",
    "prohibited content",
    "flagged",
    "moderation",
)


def _looks_like_provider_content_refusal(error: str) -> bool:
    """Отказ fal по контент-политике, отличённый от прочих провайдерских сбоев."""
    lowered = error.lower()
    return any(marker in lowered for marker in _FAL_CONTENT_POLICY_MARKERS)


# == "app.media_generation.service"

_REFUND_REASON = "media_generation_failed"

# ADR-105 §B4: the `error` of a job closed by the deadline. Matches none of
# `_FAL_CONTENT_POLICY_MARKERS`, so `_fail` does not classify it as a content-policy refusal.
DEADLINE_EXCEEDED_ERROR = "generation did not complete in time"

# ADR-105 §B5: `lastObservation` — the CAUSE dimension of the deadline event, separate from the
# consequence (always `failed` + refund). Each value is chosen by a predicate over the facts of the
# poll, never by judgement; the predicates are mutually exclusive and cover every outcome of §B2
# step 2.
OBSERVATION_UPSTREAM_ERROR = "upstream_error"  # key set, FalClient.status/result raised
OBSERVATION_UPSTREAM_PENDING = "upstream_pending"  # FalClient.status: non-terminal status
OBSERVATION_MODERATION_UNAVAILABLE = "moderation_unavailable"  # assets in, _moderate_output raised
OBSERVATION_NOT_CONFIGURED = "not_configured"  # FAL_API_KEY empty — no request went upstream
OBSERVATION_INTERNAL_ERROR = "internal_error"  # raised outside FalClient and _moderate_output


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


@dataclass(frozen=True)
class MediaAsset:
    """One generated file as returned to the client."""

    url: str
    content_type: str | None
    file_name: str | None


@dataclass(frozen=True)
class UploadedFile:
    """A reference image stored with the provider, as returned by ``POST /v1/media/uploads``."""

    url: str
    media_type: str
    size: int
    expires_at: datetime.datetime | None


@dataclass(frozen=True)
class MediaJobsFeed:
    """One page of the generations feed, as returned to the API layer."""

    items: list[MediaJobView]
    next_cursor: str | None


@dataclass(frozen=True)
class MediaJobView:
    """Projection of a job for the API layer (no ORM object crosses into the router)."""

    job: MediaJob
    assets: list[MediaAsset]


class MediaGenerationService:
    def __init__(
        self,
        *,
        repo: MediaJobsRepository,
        fal: FalClient,
        wallet: WalletService,
        settings: Settings,
        push: MediaPushService | None = None,
        request_logs: RequestLogWriter | None = None,
        moderation: ModerationService | None = None,
    ) -> None:
        self._repo = repo
        self._fal = fal
        self._wallet = wallet
        self._settings = settings
        self._push = push
        self._request_logs = request_logs
        # ADR-086: None только в тестах/легаси-сборке графа зависимостей — тогда вердикт unchecked.
        self._moderation = moderation

    # ---- pricing ----

    # Базовой цены реестра у сервиса больше НЕТ отдельным методом: показывать её пользователю
    # нельзя (она не читает операторский тариф — ADR-099 §5.1), а внутри сервиса цену считает
    # только `price_of`. Единственное определение реестровой базы живёт в
    # `instance_config.base_credits_for`; цена ДЛЯ ПОКАЗА — `instance_config.media_base_credits`.

    def price_of(
        self,
        *,
        model: FalModel,
        num_images: int | None = None,
        duration: str | None = None,
        resolution: str | None = None,
        generate_audio: bool | None = None,
    ) -> int:
        """What this particular run costs — quality tiers and usage, never from the request price.

        Image: per-resolution credits × numImages. Video: pack price × duration packs × (Veo)
        resolution/audio multipliers. text-to-* vs image-to-* does not change the price.
        """
        # ADR-099 §4: цена берётся из ячейки операторского тарифа, если она настроена, и из
        # сегодняшней формулы, если нет. Пустой оверлей воспроизводит прежнее списание
        # бит-в-бит — это свойство конструкции, а не проверка сидов.
        return instance_config.media_run_price(
            model=model,
            num_images=num_images,
            duration=duration,
            resolution=resolution,
            generate_audio=generate_audio,
            settings=self._settings,
        )

    # ---- submit ----

    async def submit(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        model_id: str,
        prompt: str,
        image_urls: list[str],
        params: dict[str, Any],
        source_job_id: uuid.UUID | None = None,
    ) -> MediaJobView:
        model = self._resolve_model(model_id=model_id, kind=kind)
        # ADR-086 §4: модерируются только URL, пришедшие ОТ КЛИЕНТА. Ассеты, подставленные из
        # sourceJobId, — наши, уже прошедшие пост-модерацию, и повторно не проверяются.
        client_image_urls = list(image_urls) if source_job_id is None else []
        if source_job_id is not None:
            if image_urls:
                # Two ways to name the same input on one request would make "which wins?" a
                # question the contract has to answer. It should not have to.
                raise ValidationFailedError(
                    "sourceJobId and imageUrls/imageUrl are mutually exclusive"
                )
            image_urls = await self._assets_of_source(
                user_id=user_id, source_job_id=source_job_id, limit=model.max_input_images
            )
        if image_urls and model.kind == KIND_VIDEO:
            # Kling/Veo fetch the still themselves. fal image-result URLs are often too large
            # (20 MB / 8:1 PNG) and fail as ``body.image_url: Failed to download the file``.
            # Rehost a compact JPEG on fal-cdn-v3 BEFORE the debit so a failed copy is free.
            image_urls = [await self._fal.rehost_reference_image(url) for url in image_urls]
        variant = model.variant_for(with_image=bool(image_urls))
        if variant is None:
            raise ValidationFailedError(f"model {model.id} does not accept a reference image")
        if len(image_urls) > model.max_input_images:
            raise ValidationFailedError(
                f"model {model.id} accepts at most {model.max_input_images} reference image(s)"
            )
        # Validated against the VARIANT, not the model: the same parameter accepts different values
        # in different modes (Veo allows aspectRatio "auto" only with a reference image), and a
        # parameter the mode has no notion of must not be silently swallowed.
        for parameter in ("aspectRatio", "resolution", "duration"):
            self._validate_enum(parameter, params.get(parameter), variant)

        # One resolved mapping feeds BOTH the price and the upstream payload (ADR-061 §3). Pricing
        # the raw request while letting fal fill the blanks meant billing a cheaper run than the
        # one we asked for — fal defaults generate_audio to true and Veo's duration to 8s.
        values = resolve_values(variant=variant, values={"prompt": prompt, **params})
        payload = build_fal_input(
            model=model,
            variant=variant,
            values=values,
            image_urls=image_urls,
        )

        # The job id is minted here (not by the DB default) because it is also the wallet
        # idempotency key — the debit must be attributable to the job before the row exists.
        job_id = uuid.uuid4()
        cost = self.price_of(
            model=model,
            num_images=_as_int(values.get("numImages")),
            duration=_as_str(values.get("duration")),
            resolution=_as_str(values.get("resolution")),
            generate_audio=_as_bool(values.get("generateAudio")),
        )
        # Same resolved values, second question: what does the run cost US (ADR-079). Recorded
        # here because this is the only place that knows them — `media_jobs` keeps the credits
        # but not the knobs, so afterwards the fal bill is only recoverable up to a credit pack.
        provider_cost_usd = round_usd(
            media_cost_usd_of_run(
                model=model,
                num_images=_as_int(values.get("numImages")),
                duration=_as_str(values.get("duration")),
                resolution=_as_str(values.get("resolution")),
                generate_audio=_as_bool(values.get("generateAudio")),
            )
        )
        # ADR-086 §4: модерация входа ОБЯЗАНА стоять до списания — иначе повторяется ровно тот
        # дефект, из-за которого написан багрепорт: за отклонённый контент уже списаны кредиты.
        input_verdict = await self._moderate_input(prompt=prompt, image_urls=client_image_urls)

        await self._wallet.consume(
            user_id=user_id,
            amount=cost,
            idempotency_key=f"media-gen:{job_id}",
            meta={"source": "media_generation", "model": model.id, "kind": model.kind},
        )

        submission = await self._fal.submit(endpoint=variant.endpoint, payload=payload)
        job = await self._repo.create(
            job_id=job_id,
            user_id=user_id,
            model_id=model.id,
            kind=model.kind,
            fal_endpoint=variant.endpoint,
            fal_request_id=submission.request_id,
            status_url=submission.status_url,
            response_url=submission.response_url,
            status=STATUS_QUEUED,
            prompt=prompt,
            credits_charged=cost,
            provider_cost_usd=provider_cost_usd,
            parent_job_id=source_job_id,
            input_image_urls=list(image_urls) or None,
            moderation=input_verdict.to_payload(),
        )
        log_event(
            logger,
            logging.INFO,
            "media_generation_submitted",
            userId=str(user_id),
            jobId=str(job_id),
            model=model.id,
            kind=model.kind,
            credits=cost,
            falEndpoint=variant.endpoint,
        )
        return MediaJobView(job=job, assets=[])

    async def _moderate_input(self, *, prompt: str, image_urls: list[str]) -> ModerationVerdict:
        """Пре-модерация промпта и клиентского референса (ADR-086 §4).

        `blocked` → 422 content_policy_violation до единого списания. `flagged` вход проходит
        дальше намеренно (§6): блокировать по flagged значило бы поток ложных отказов на
        безобидных формулировках.
        """
        if self._moderation is None:
            return unchecked_verdict()
        verdict = await self._moderation.check(
            surface=SURFACE_MEDIA_SUBMIT,
            stage=STAGE_INPUT,
            text=prompt,
            image_urls=image_urls,
        )
        if verdict.blocked:
            raise ContentPolicyViolationError(
                "запрос отклонён правилами контента: измените описание или референс"
            )
        return verdict

    async def job_exists(self, *, user_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        """Есть ли такая задача у этого владельца — без обращения к провайдеру.

        `get_job` у незавершённой задачи опрашивает провайдера и может упасть на его аварии.
        Для ответа на вопрос «идентификатор выдуман или нет» внешний вызов не нужен: хватает
        строки в нашей базе. Owner-scoped, поэтому чужая задача неотличима от отсутствующей.
        """
        return await self._repo.get(job_id=job_id, user_id=user_id) is not None

    async def _assets_of_source(
        self, *, user_id: uuid.UUID, source_job_id: uuid.UUID, limit: int
    ) -> list[str]:
        """Reference URLs taken from an earlier generation of this user (ADR-063 §1).

        The client sends a job id rather than a URL because the edit chain is a relation between
        OUR jobs, while the provider's URL lives under its own retention policy; keyed by id, the
        link in the feed stays true even after the link dies.
        """
        source = await self._repo.get(job_id=source_job_id, user_id=user_id)
        if source is None:
            # Owner-scoped: a foreign job must be indistinguishable from a missing one.
            raise NotFoundError("media job not found")
        if source.status != STATUS_COMPLETED:
            raise ValidationFailedError("sourceJobId must reference a completed generation")
        if source.kind != KIND_IMAGE:
            # Both editing and image-to-video take a picture in; we do not extract video frames.
            raise ValidationFailedError("sourceJobId must reference an image generation")
        urls = [asset.url for asset in _assets_from_result(source.result)]
        if not urls:
            raise ValidationFailedError("sourceJobId references a generation with no output")
        return urls[: max(1, limit)]

    # ---- reference-image upload ----

    async def upload_reference_image(
        self, *, media_type: str, file_name: str, data: str
    ) -> UploadedFile:
        """Store a client's local photo with the provider and return a URL it can generate from.

        Exists because ``imageUrls``/``imageUrl`` accept only https URLs — fal fetches the picture
        itself — while a phone only ever has local bytes (ADR-062). Costs no credits: the charge
        belongs to the generation, and making a mis-picked reference cost money would be absurd.

        Limits are checked BEFORE decoding (the base64 length bounds the decoded size), and the
        magic bytes are checked after, so a renamed file cannot pass as an image.
        """
        if _decoded_len_from_base64(data) > self._settings.media_upload_max_bytes:
            raise PayloadTooLargeError("file exceeds the maximum allowed size")
        content = _decode_base64(data)
        if len(content) > self._settings.media_upload_max_bytes:
            raise PayloadTooLargeError("file exceeds the maximum allowed size")
        _check_magic_bytes(media_type, content)

        # ADR-086 §2: загруженный референс — пользовательские байты, которые станут входом платной
        # генерации, поэтому проверяются здесь, ДО отправки провайдеру. Проверяем после валидации
        # (кривой файл дешевле отбить раньше) и по data-URI, а не по URL: URL ещё не существует.
        if self._moderation is not None:
            verdict = await self._moderation.check(
                surface=SURFACE_MEDIA_UPLOAD,
                stage=STAGE_INPUT,
                image_urls=[f"data:{media_type};base64,{data}"],
            )
            if verdict.blocked:
                raise ContentPolicyViolationError("изображение отклонено правилами контента")

        url = await self._fal.upload(content=content, media_type=media_type, file_name=file_name)
        return UploadedFile(
            url=url, media_type=media_type, size=len(content), expires_at=self._expires_at()
        )

    def _expires_at(self) -> datetime.datetime | None:
        """When the uploaded file dies, if the instance pinned a lifetime (ADR-061 §5).

        ``None`` covers both "never expires" and "provider decides": in either case we have no
        honest timestamp to give, and inventing fal's default here would go stale silently.
        """
        preference = self._settings.fal_asset_retention()
        if not isinstance(preference, int) or isinstance(preference, bool):
            return None
        return datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=preference)

    # ---- read / poll ----

    async def get_job(self, *, user_id: uuid.UUID, job_id: uuid.UUID) -> MediaJobView:
        job = await self._repo.get(job_id=job_id, user_id=user_id)
        if job is None:
            raise NotFoundError("media job not found")
        if job.status in TERMINAL_STATUSES:
            return MediaJobView(job=job, assets=_assets_from_result(job.result))
        return await self._advance(job)

    async def get_stored_asset(
        self, *, job_id: uuid.UUID, index: int
    ) -> tuple[MediaJob, MediaAsset]:
        """Stored fal asset for the signed download route. Missing/OOB → 404."""
        job = await self._repo.get_by_id(job_id)
        if job is None:
            raise NotFoundError("media job not found")
        assets = _assets_from_result(job.result)
        if index < 0 or index >= len(assets):
            raise NotFoundError("media job not found")
        return job, assets[index]

    async def advance(self, job: MediaJob) -> MediaJobView:
        """Advance one already-loaded job (used by the background reconciler, ADR-067)."""
        if job.status in TERMINAL_STATUSES:
            return MediaJobView(job=job, assets=_assets_from_result(job.result))
        return await self._advance(job)

    async def list_jobs(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        kind: str | None,
        cursor: MediaJobCursor | None = None,
    ) -> MediaJobsFeed:
        """One page of the user's feed, newest-first. Read-only — never polls upstream.

        Listing N jobs must not fan out into N upstream calls, so a non-terminal job is reported
        with its last known status; the client refreshes the one it cares about via
        ``GET /v1/media/jobs/{id}``.
        """
        page = await self._repo.list_for_user(
            user_id=user_id, limit=limit, kind=kind, cursor=cursor
        )
        return MediaJobsFeed(
            items=[
                MediaJobView(job=row, assets=_assets_from_result(row.result)) for row in page.items
            ],
            next_cursor=page.next_cursor,
        )

    async def delete_job(self, *, user_id: uuid.UUID, job_id: uuid.UUID) -> None:
        """Remove one finished job from the feed (ADR-063 §4).

        A queued/running job is refused: the refund for a run the provider fails is attributed to
        this row and triggered by polling it, so deleting it first would destroy the only place
        that refund can happen. Only our row goes — the asset stays with the provider until its
        own retention expires, since we never owned those bytes.
        """
        job = await self._repo.get(job_id=job_id, user_id=user_id)
        if job is None:
            raise NotFoundError("media job not found")
        if job.status not in TERMINAL_STATUSES:
            raise JobNotTerminalError(
                "job is still running; poll it until it completes or fails before deleting"
            )
        await self._repo.delete(job)
        log_event(
            logger,
            logging.INFO,
            "media_generation_deleted",
            userId=str(user_id),
            jobId=str(job_id),
            model=job.model_id,
            status=job.status,
        )

    async def _advance(self, job: MediaJob) -> MediaJobView:
        """Poll fal once and persist any state transition (ADR-060 §3, ADR-105 §B2).

        A ``422`` while polling is a *rejected run*, not a bad poll: fal validates some inputs only
        while executing (a reference image it cannot download, for instance) and then serves that
        verdict from the status/result URL forever. Re-raising it would answer a perfectly valid
        ``GET /v1/media/jobs/{id}`` with ``422``, leave the job non-terminal for good and never
        refund — so it is folded into the normal failure path (terminal ``failed`` + refund) with
        fal's own wording kept as ``error``.

        ADR-105 §B2 — the poll ALWAYS happens, past the deadline too (last chance): a terminal
        answer is applied exactly as before. When the poll yields NO final state — for ANY reason
        (an exception of the fal client, a non-terminal status, an unavailable post-moderation, an
        empty key, an exception of our own code between the poll and the outcome) —
        ``_close_if_overdue`` decides: a job older than ``MEDIA_JOB_DEADLINE_SECONDS`` is closed as
        ``failed`` with a refund and the exception does not surface; a younger job is not touched
        (the exception propagates, a non-terminal status marks it ``running``) — the next poll
        retries, as before.
        """
        try:
            status = await self._fal.status(status_url=job.status_url, endpoint=job.fal_endpoint)
        except ValidationFailedError as exc:
            return await self._fail(job, error=exc.message)
        except UpstreamJobGoneError as exc:
            # 404 — задача исчезла у провайдера навсегда. Это отказ с точки зрения
            # пользователя: он заплатил и не получит результата, значит кредиты возвращаются.
            # Повторять нечего, иначе задача остаётся незавершённой вечно.
            return await self._fail(job, error=exc.message)
        except Exception as exc:
            closed = await self._close_if_overdue(
                job, observation=self._fal_failure_observation(), cause=exc
            )
            if closed is None:
                raise
            return closed

        if status.status == FAL_COMPLETED:
            try:
                body = await self._fal.result(
                    response_url=job.response_url, endpoint=job.fal_endpoint
                )
            except ValidationFailedError as exc:
                return await self._fail(job, error=exc.message)
            except Exception as exc:
                closed = await self._close_if_overdue(
                    job, observation=self._fal_failure_observation(), cause=exc
                )
                if closed is None:
                    raise
                return closed
            try:
                result = _normalize_result(body, kind=job.kind)
                assets = _assets_from_result(result)
            except Exception as exc:
                closed = await self._close_if_overdue(
                    job, observation=OBSERVATION_INTERNAL_ERROR, cause=exc
                )
                if closed is None:
                    raise
                return closed
            if not assets:
                # COMPLETED with nothing usable is a failed run from the user's point of view.
                return await self._fail(job, error="generation produced no output")
            # ADR-086 §5: пост-модерация результата. Только image — omni-moderation не принимает
            # видео; у видео-задачи moderation отражает вход (Q-086-2). Проверка ДО mark_completed,
            # чтобы заблокированный ассет никогда не оказался в терминальном completed.
            try:
                output_verdict = await self._moderate_output(job, assets)
            except Exception as exc:
                closed = await self._close_if_overdue(
                    job, observation=OBSERVATION_MODERATION_UNAVAILABLE, cause=exc
                )
                if closed is None:
                    raise
                return closed
            if output_verdict is not None and output_verdict.blocked:
                return await self._blocked_by_moderation(job, verdict=output_verdict)
            await self._repo.mark_completed(
                job,
                result=result,
                moderation=None if output_verdict is None else output_verdict.to_payload(),
            )
            if self._request_logs is not None:
                await self._request_logs.finish_media(
                    media_job_id=job.id, failed=False, refunded=False
                )
            log_event(
                logger,
                logging.INFO,
                "media_generation_completed",
                userId=str(job.user_id),
                jobId=str(job.id),
                model=job.model_id,
                assets=len(assets),
            )
            if self._push is not None and assets:
                await self._push.notify_media_ready(
                    job_id=job.id,
                    user_id=job.user_id,
                    kind=job.kind,
                    media_url=public_asset_url(
                        job_id=job.id,
                        owner_user_id=job.user_id,
                        index=0,
                        stored_url=assets[0].url,
                    ),
                )
            return MediaJobView(job=job, assets=assets)

        if status.status in (FAL_FAILED, FAL_CANCELED):
            return await self._fail(job, error=status.error or "generation failed upstream")

        closed = await self._close_if_overdue(job, observation=OBSERVATION_UPSTREAM_PENDING)
        if closed is not None:
            return closed
        await self._repo.mark_running(job)
        return MediaJobView(job=job, assets=[])

    def _fal_failure_observation(self) -> str:
        """``lastObservation`` of a failed ``FalClient.status``/``result`` call (ADR-105 §B5).

        Predicates from the facts of the poll, mutually exclusive: an empty ``FAL_API_KEY`` means no
        request went upstream at all (``FalClient._headers`` refuses first) ⇒ ``not_configured``;
        a key is set and the call raised ⇒ ``upstream_error`` — a rejected key (``401``/``403``)
        included, because that IS an answer from fal.
        """
        return OBSERVATION_UPSTREAM_ERROR if self._fal.configured else OBSERVATION_NOT_CONFIGURED

    def _age(self, job: MediaJob) -> datetime.timedelta | None:
        created_at = job.created_at
        if created_at is None:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=datetime.UTC)
        return _now() - created_at

    async def _close_if_overdue(
        self,
        job: MediaJob,
        *,
        observation: str,
        cause: BaseException | None = None,
    ) -> MediaJobView | None:
        """The deadline branch of ADR-105 §B2: close an overdue job, leave a young one alone.

        Called ONLY when the poll yielded no final state. ``now − created_at >
        MEDIA_JOB_DEADLINE_SECONDS`` ⇒ ``media_generation_deadline_exceeded`` and ``_fail`` with
        ``DEADLINE_EXCEEDED_ERROR`` (refund keyed ``media-refund:{jobId}``, ``mark_failed``,
        ``request_logs.finish_media``) — and the view is returned, so no exception surfaces.
        Otherwise ``None``: the caller keeps today's behaviour (re-raise / ``mark_running``).
        """
        age = self._age(job)
        if age is None or age.total_seconds() <= self._settings.media_job_deadline_seconds:
            return None
        fields: dict[str, Any] = {
            "jobId": str(job.id),
            "model": job.model_id,
            "ageSeconds": int(age.total_seconds()),
            "lastObservation": observation,
        }
        upstream_status = upstream_status_of(cause) if cause is not None else None
        if upstream_status is not None:
            fields["upstreamStatus"] = upstream_status
        log_event(logger, logging.WARNING, "media_generation_deadline_exceeded", **fields)
        return await self._fail(job, error=DEADLINE_EXCEEDED_ERROR)

    async def _moderate_output(
        self, job: MediaJob, assets: list[MediaAsset]
    ) -> ModerationVerdict | None:
        """Пост-модерация результата (ADR-086 §5). None = проверка неприменима, вердикт не меняем.

        Видео не проверяется: провайдер модерации не принимает видеофайл. Отклонение промпта самим
        fal приходит, как и раньше, обычным ``failed`` с текстом провайдера.
        """
        if self._moderation is None or job.kind != KIND_IMAGE:
            return None
        return await self._moderation.check(
            surface=SURFACE_MEDIA_RESULT,
            stage=STAGE_OUTPUT,
            image_urls=[a.url for a in assets],
        )

    async def _blocked_by_moderation(
        self, job: MediaJob, *, verdict: ModerationVerdict
    ) -> MediaJobView:
        """Результат отклонён модерацией: терминал без ассетов + возврат кредитов (ADR-086 §5).

        Статус — существующий ``failed``: ``MediaJobResponse.status`` закрытый Literal, и новое
        значение сломало бы декодеры уже выпущенных iOS-сборок. Исход различает поле ``moderation``.
        Ассеты НЕ сохраняются в result — иначе файл остался бы достижим по signed-URL (ADR-085).
        Push «media ready» не отправляется: он идёт только по ветке mark_completed.
        """
        refunded = job.credits_refunded
        if not refunded and job.credits_charged > 0:
            await self._wallet.grant(
                user_id=job.user_id,
                amount=job.credits_charged,
                idempotency_key=f"media-refund:{job.id}",
                meta={"source": "media_generation_refund", "model": job.model_id},
                reason=_REFUND_REASON,
            )
            refunded = True
        await self._repo.mark_failed(
            job,
            # error — человекочитаемый текст: выпущенные iOS-сборки показывают его пользователю
            # как есть, а внутренние идентификаторы в user-facing текст не попадают
            # (08-api-documentation.md R9.4). Машинный признак несёт moderation.status="blocked".
            error="Результат отклонён правилами контента",
            refunded=refunded,
            moderation=verdict.to_payload(),
            result={"assets": []},
        )
        if self._request_logs is not None:
            await self._request_logs.finish_media(
                media_job_id=job.id, failed=True, refunded=refunded
            )
        log_event(
            logger,
            logging.WARNING,
            "media_generation_blocked",
            userId=str(job.user_id),
            jobId=str(job.id),
            model=job.model_id,
            categories=list(verdict.categories),
            refundedCredits=job.credits_charged if refunded else 0,
        )
        return MediaJobView(job=job, assets=[])

    async def _fail(self, job: MediaJob, *, error: str) -> MediaJobView:
        """Mark the run failed and refund its credits (once, idempotently).

        ADR-086 §5: если провайдер отклонил запуск по СВОЕЙ контент-политике, это единственный
        наблюдаемый сигнал модерации выхода для видео — он превращается в
        ``moderation.status = "blocked"`` вместо безликого ``failed``, чтобы клиент показал
        заглушку, а не «ошибку генерации». Кредиты возвращаются в любом случае, тем же ключом.
        """
        refunded = job.credits_refunded
        if not refunded and job.credits_charged > 0:
            await self._wallet.grant(
                user_id=job.user_id,
                amount=job.credits_charged,
                idempotency_key=f"media-refund:{job.id}",
                meta={"source": "media_generation_refund", "model": job.model_id},
                reason=_REFUND_REASON,
            )
            refunded = True
        provider_refusal = _looks_like_provider_content_refusal(error)
        moderation_payload = None
        if provider_refusal:
            moderation_payload = ModerationVerdict(
                status=STATUS_BLOCKED,
                stage=STAGE_OUTPUT,
                categories=(),
                checked_at=datetime.datetime.now(datetime.UTC),
                provider="fal",
                model=job.model_id,
            ).to_payload()
            moderation_decisions_total.labels(
                surface=SURFACE_MEDIA_RESULT, stage=STAGE_OUTPUT, decision=STATUS_BLOCKED
            ).inc()
        await self._repo.mark_failed(
            job, error=error[:500], refunded=refunded, moderation=moderation_payload
        )
        if self._request_logs is not None:
            await self._request_logs.finish_media(
                media_job_id=job.id, failed=True, refunded=refunded
            )
        log_event(
            logger,
            logging.WARNING,
            "media_generation_failed",
            userId=str(job.user_id),
            jobId=str(job.id),
            model=job.model_id,
            refundedCredits=job.credits_charged if refunded else 0,
        )
        return MediaJobView(job=job, assets=[])

    # ---- helpers ----

    @staticmethod
    def _resolve_model(*, model_id: str, kind: str) -> FalModel:
        model = find_model(model_id)
        if model is None:
            raise ValidationFailedError(f"unknown model: {model_id}")
        if model.kind != kind:
            # Posting a video model to /v1/media/images (or vice versa) is a client mistake worth
            # naming explicitly — the two routes exist precisely because the inputs differ.
            expected = "images" if kind == KIND_IMAGE else "videos"
            actual = "images" if model.kind == KIND_IMAGE else "videos"
            raise ValidationFailedError(
                f"model {model.id} generates {actual}, not {expected}; use /v1/media/{actual}"
            )
        return model

    @staticmethod
    def _validate_enum(field: str, value: Any, variant: FalVariant) -> None:
        """Reject a value this mode does not support, before spending credits."""
        if value is None:
            return
        allowed = variant.allowed(field)
        if not allowed:
            raise ValidationFailedError(f"{field} is not supported by this model in this mode")
        if value not in allowed:
            raise ValidationFailedError(f"{field} must be one of: {', '.join(allowed)}")


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _normalize_result(body: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """Project a model-specific fal output into our stable ``{assets: [...]}`` shape.

    Image models return ``images: [{url, content_type, file_name}]``, video models a single
    ``video: {url}``. Normalizing at the boundary keeps the wire contract identical across models
    and keeps vendor field names out of the stored rows and the client.
    """
    assets: list[dict[str, Any]] = []
    if kind == KIND_IMAGE:
        for item in body.get("images") or []:
            asset = _asset_dict(item)
            if asset is not None:
                assets.append(asset)
    elif kind == KIND_VIDEO:
        asset = _asset_dict(body.get("video"))
        if asset is not None:
            assets.append(asset)
        for item in body.get("videos") or []:
            extra = _asset_dict(item)
            if extra is not None:
                assets.append(extra)

    result: dict[str, Any] = {"assets": assets}
    description = body.get("description")
    if isinstance(description, str) and description:
        result["description"] = description
    seed = body.get("seed")
    if isinstance(seed, int):
        result["seed"] = seed
    return result


def _asset_dict(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if not isinstance(url, str) or not url:
        return None
    content_type = item.get("content_type")
    file_name = item.get("file_name")
    return {
        "url": url,
        "contentType": content_type if isinstance(content_type, str) else None,
        "fileName": file_name if isinstance(file_name, str) else None,
    }


def _assets_from_result(result: dict[str, Any] | None) -> list[MediaAsset]:
    if not isinstance(result, dict):
        return []
    out: list[MediaAsset] = []
    for item in result.get("assets") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        content_type = item.get("contentType")
        file_name = item.get("fileName")
        out.append(
            MediaAsset(
                url=url,
                content_type=content_type if isinstance(content_type, str) else None,
                file_name=file_name if isinstance(file_name, str) else None,
            )
        )
    return out
