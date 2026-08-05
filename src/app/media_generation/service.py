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

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.errors import NotFoundError, ValidationFailedError
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
from app.media_generation.fal_client import (
    FAL_CANCELED,
    FAL_COMPLETED,
    FAL_FAILED,
    FalClient,
)
from app.media_generation.repository import (
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
    ) -> MediaJobView:
        model = self._resolve_model(model_id=model_id, kind=kind)
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

    # ---- read / poll ----

    async def get_job(self, *, user_id: uuid.UUID, job_id: uuid.UUID) -> MediaJobView:
        job = await self._repo.get(job_id=job_id, user_id=user_id)
        if job is None:
            raise NotFoundError("media job not found")
        if job.status in TERMINAL_STATUSES:
            return MediaJobView(job=job, assets=_assets_from_result(job.result))
        return await self._advance(job)

    async def list_jobs(
        self, *, user_id: uuid.UUID, limit: int, kind: str | None
    ) -> list[MediaJobView]:
        """List the user's jobs newest-first. Read-only — never polls upstream.

        Listing N jobs must not fan out into N upstream calls, so a non-terminal job is reported
        with its last known status; the client refreshes the one it cares about via
        ``GET /v1/media/jobs/{id}``.
        """
        rows = await self._repo.list_for_user(user_id=user_id, limit=limit, kind=kind)
        return [MediaJobView(job=row, assets=_assets_from_result(row.result)) for row in rows]

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
