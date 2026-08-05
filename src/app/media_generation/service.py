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

from app.chat.attachments import (
    _check_magic_bytes,
    _decode_base64,
    _decoded_len_from_base64,
)
from app.config import Settings
from app.errors import (
    JobNotTerminalError,
    NotFoundError,
    PayloadTooLargeError,
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
    run_price,
)
from app.media_generation.cursor import MediaJobCursor
from app.media_generation.fal_client import (
    FAL_CANCELED,
    FAL_COMPLETED,
    FAL_FAILED,
    FalClient,
)
from app.media_generation.repository import (
    STATUS_COMPLETED,
    STATUS_QUEUED,
    TERMINAL_STATUSES,
    MediaJobsRepository,
)
from app.models import MediaJob
from app.observability.logging import log_event
from app.wallet.service import WalletService

logger = logging.getLogger(__name__)  # == "app.media_generation.service"

_REFUND_REASON = "media_generation_failed"


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
    ) -> None:
        self._repo = repo
        self._fal = fal
        self._wallet = wallet
        self._settings = settings

    # ---- pricing ----

    def credits_for(self, model: FalModel) -> int:
        """Base price of one run: MEDIA_MODEL_CREDITS override, else the catalog default.

        Never derived from the request body (anti-tamper) and never zero: a non-positive override
        is dropped by ``Settings.media_model_credits()``, so the catalog default always wins.
        """
        return self._settings.media_model_credits().get(model.id, model.default_credits)

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
        return run_price(
            model=model,
            base_credits=self.credits_for(model),
            num_images=num_images,
            duration=duration,
            resolution=resolution,
            generate_audio=generate_audio,
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
            parent_job_id=source_job_id,
            input_image_urls=list(image_urls) or None,
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
        """Poll fal once and persist any state transition.

        A ``422`` while polling is a *rejected run*, not a bad poll: fal validates some inputs only
        while executing (a reference image it cannot download, for instance) and then serves that
        verdict from the status/result URL forever. Re-raising it would answer a perfectly valid
        ``GET /v1/media/jobs/{id}`` with ``422``, leave the job non-terminal for good and never
        refund — so it is folded into the normal failure path (terminal ``failed`` + refund) with
        fal's own wording kept as ``error``. Transient upstream problems (``429``/``502``) still
        propagate: the job stays non-terminal and the next poll retries.
        """
        try:
            status = await self._fal.status(status_url=job.status_url, endpoint=job.fal_endpoint)
        except ValidationFailedError as exc:
            return await self._fail(job, error=exc.message)

        if status.status == FAL_COMPLETED:
            try:
                body = await self._fal.result(
                    response_url=job.response_url, endpoint=job.fal_endpoint
                )
            except ValidationFailedError as exc:
                return await self._fail(job, error=exc.message)
            result = _normalize_result(body, kind=job.kind)
            assets = _assets_from_result(result)
            if not assets:
                # COMPLETED with nothing usable is a failed run from the user's point of view.
                return await self._fail(job, error="generation produced no output")
            await self._repo.mark_completed(job, result=result)
            log_event(
                logger,
                logging.INFO,
                "media_generation_completed",
                userId=str(job.user_id),
                jobId=str(job.id),
                model=job.model_id,
                assets=len(assets),
            )
            return MediaJobView(job=job, assets=assets)

        if status.status in (FAL_FAILED, FAL_CANCELED):
            return await self._fail(job, error=status.error or "generation failed upstream")

        await self._repo.mark_running(job)
        return MediaJobView(job=job, assets=[])

    async def _fail(self, job: MediaJob, *, error: str) -> MediaJobView:
        """Mark the run failed and refund its credits (once, idempotently)."""
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
        await self._repo.mark_failed(job, error=error[:500], refunded=refunded)
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
