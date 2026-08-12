"""Global (project-independent) server-side tool handlers (ADR-026 / ADR-064 / ADR-068 / ADR-070).

Unlike SiteToolHandlers (project-scoped site.*, ADR-011), these handlers are NOT tied to a
WebsiteService/project — they execute in the chat tool-loop without an external_project_id and
are offered to Claude without requiring a project (including «чистый чат» with no project,
ADR-022). Mode-gating (axis C) is orthogonal: ``quiz.generate`` is offered only in
``study_learn``; ``time.now`` and ``media.*`` are offered in every mode.

``time.now`` (ADR-026 §6) returns the current date/time via an injectable ``Clock``.
``quiz.generate`` (ADR-064) validates and echoes a quiz pool.
``media.generate_image`` / ``media.generate_video`` (ADR-068) call ``MediaGenerationService.submit``
— submit-only, never wait for fal completion. Failures become ``ToolExecution.error`` so the
chat turn survives (same soft contract as ``invalid_timezone`` / ``invalid_quiz``).
``media.ask_params`` (ADR-070) starts a catalog-backed choices wizard.

When the user attached images on the SAME turn, media tools upload them to fal (ADR-062) and
use them as image-to-image / image-to-video references unless the model already passed
``sourceJobId`` / ``imageUrls``.

The same ``ToolExecution`` contract as SiteToolHandlers is reused (single tool-result contract for
the orchestrator). Only the frozen dataclass is imported from website.tools — no website
infrastructure is instantiated here (ADR-026 §5).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from app.chat.attachment_refs import MEDIA_NO_RECENT_IMAGE_ERROR_CODE
from app.chat.attachments import ImageAttachmentRef
from app.chat.media_choices import build_wizard_state
from app.chat.tools import (
    QUIZ_CONSTRAINTS_HINT,
    QUIZ_INVALID_ERROR_CODE,
    TIME_NOW_TZ_MAX_LENGTH,
    TOOL_MEDIA_ASK_PARAMS,
    TOOL_MEDIA_GENERATE_IMAGE,
    TOOL_MEDIA_GENERATE_VIDEO,
    TOOL_QUIZ_GENERATE,
    TOOL_TIME_NOW,
    Quiz,
    content_free_args_error,
)
from app.errors import (
    InsufficientCreditsError,
    MediaGenerationNotConfiguredError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamError,
    ValidationFailedError,
)
from app.media_generation.service import MediaGenerationService
from app.website.tools import ToolExecution

# English weekday names by UTC date (Monday..Sunday), ADR-026 §6.
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

MEDIA_INVALID_ERROR_CODE = "invalid_media_request"
MEDIA_NOT_CONFIGURED_ERROR_CODE = "media_not_configured"
MEDIA_INSUFFICIENT_CREDITS_ERROR_CODE = "insufficient_credits"
MEDIA_UPSTREAM_ERROR_CODE = "media_upstream_error"

# Cap auto-uploads from chat attachments (matches media.generate_image imageUrls max).
_TURN_IMAGE_UPLOAD_MAX = 14


@runtime_checkable
class Clock(Protocol):
    """Injectable source of the current time (ADR-026 §8).

    ``now()`` MUST return a timezone-aware UTC ``datetime``. The default implementation is
    ``SystemClock``; tests inject a ``FixedClock`` for determinism.
    """

    def now(self) -> datetime.datetime: ...


class SystemClock:
    """Default Clock: the real wall-clock time in UTC (ADR-026 §8)."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(tz=datetime.UTC)


class GlobalToolHandlers:
    """Dispatch + handlers for global server-side tools (ADR-026 / ADR-068).

    Project-independent: no WebsiteService, no external_project_id, no session-context args. Time is
    taken from the injected ``Clock`` (default ``SystemClock``). Media tools use the optional
    ``MediaGenerationService`` (None → ``media_not_configured`` tool-result error).
    """

    def __init__(
        self,
        clock: Clock | None = None,
        media: MediaGenerationService | None = None,
    ) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._media = media

    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        user_id: uuid.UUID | None = None,
        turn_images: list[ImageAttachmentRef] | None = None,
        recent_image_urls: list[str] | None = None,
        last_image_job_id: str | None = None,
    ) -> ToolExecution:
        """Execute a global server-side tool. Returns a ToolExecution (result or error envelope)."""
        if tool_name == TOOL_TIME_NOW:
            return self._time_now(args)
        if tool_name == TOOL_QUIZ_GENERATE:
            return self._quiz_generate(args)
        if tool_name == TOOL_MEDIA_ASK_PARAMS:
            return await self._media_ask_params(
                args,
                turn_images=turn_images,
                recent_image_urls=recent_image_urls,
                last_image_job_id=last_image_job_id,
            )
        if tool_name == TOOL_MEDIA_GENERATE_IMAGE:
            return await self._media_generate(
                kind="image",
                args=args,
                user_id=user_id,
                turn_images=turn_images,
                recent_image_urls=recent_image_urls,
            )
        if tool_name == TOOL_MEDIA_GENERATE_VIDEO:
            return await self._media_generate(
                kind="video",
                args=args,
                user_id=user_id,
                turn_images=turn_images,
                recent_image_urls=recent_image_urls,
            )
        # Unknown global tool name — should never happen (validated upstream against the registry).
        return ToolExecution.error("unknown_tool", f"unknown global server-side tool: {tool_name}")

    async def _upload_turn_images(
        self, turn_images: list[ImageAttachmentRef] | None
    ) -> list[str] | ToolExecution:
        """Upload same-turn chat image attachments to fal; return https URLs or a soft error."""
        if not turn_images:
            return []
        if self._media is None:
            return ToolExecution.error(
                MEDIA_NOT_CONFIGURED_ERROR_CODE,
                "media generation is not configured on this instance",
            )
        urls: list[str] = []
        try:
            for img in turn_images[:_TURN_IMAGE_UPLOAD_MAX]:
                uploaded = await self._media.upload_reference_image(
                    media_type=img.media_type,
                    file_name=img.filename,
                    data=img.data,
                )
                urls.append(uploaded.url)
        except PayloadTooLargeError as exc:
            return ToolExecution.error(MEDIA_INVALID_ERROR_CODE, str(exc)[:400])
        except ValidationFailedError as exc:
            return ToolExecution.error(MEDIA_INVALID_ERROR_CODE, str(exc)[:400])
        except MediaGenerationNotConfiguredError:
            return ToolExecution.error(
                MEDIA_NOT_CONFIGURED_ERROR_CODE,
                "media generation is not configured on this instance",
            )
        except UpstreamError:
            return ToolExecution.error(
                MEDIA_UPSTREAM_ERROR_CODE, "media provider unavailable; try again later"
            )
        return urls

    async def _media_ask_params(
        self,
        args: dict[str, Any],
        *,
        turn_images: list[ImageAttachmentRef] | None = None,
        recent_image_urls: list[str] | None = None,
        last_image_job_id: str | None = None,
    ) -> ToolExecution:
        """Start a mediaChoices wizard; options come only from the server catalog (ADR-070)."""
        kind = str(args.get("kind") or "")
        prompt = str(args.get("prompt") or "")
        source_raw = args.get("sourceJobId")
        source_job_id: str | None = None
        if source_raw is not None:
            try:
                source_job_id = str(uuid.UUID(str(source_raw)))
            except ValueError:
                return ToolExecution.error(MEDIA_INVALID_ERROR_CODE, "sourceJobId must be a UUID")

        image_urls: list[str] = []
        use_recent = bool(args.get("useRecentImage"))
        # Prefer an explicit prior job; otherwise bridge chat attachments → fal https URLs.
        if source_job_id is None and turn_images:
            uploaded = await self._upload_turn_images(turn_images)
            if isinstance(uploaded, ToolExecution):
                return uploaded
            image_urls = uploaded
        elif source_job_id is None and use_recent:
            urls = [u for u in (recent_image_urls or []) if isinstance(u, str) and u]
            if not urls:
                return ToolExecution.error(
                    MEDIA_NO_RECENT_IMAGE_ERROR_CODE,
                    "no recent chat photo available (expired or missing); "
                    "ask the user to re-attach",
                )
            image_urls = urls

        # Offer «Использовать последнее фото?» only when starting video without a chosen reference.
        offer_last = (
            last_image_job_id
            if (kind == "video" and not source_job_id and not image_urls)
            else None
        )

        def _credits(model: Any) -> int:
            if self._media is not None:
                return self._media.credits_for(model)
            return int(model.default_credits)

        selection_id = str(uuid.uuid4())
        try:
            state = build_wizard_state(
                selection_id=selection_id,
                kind=kind,
                prompt=prompt,
                source_job_id=source_job_id,
                image_urls=image_urls or None,
                last_image_job_id=offer_last,
                answers={},
                credits_for=_credits,
            )
        except ValueError as exc:
            return ToolExecution.error(MEDIA_INVALID_ERROR_CODE, str(exc)[:400])
        if state is None:
            return ToolExecution.error(
                MEDIA_INVALID_ERROR_CODE, "media catalog has no steps for this request"
            )
        return ToolExecution.ok(state)

    @staticmethod
    def _quiz_generate(args: dict[str, Any]) -> ToolExecution:
        """Validate the quiz pool and echo it back as the tool result (ADR-064 §2, §5).

        "Execution" of quiz.generate is validation + echo: the pool is produced by the model, the
        backend only guarantees the contract no provider can guarantee (counts, lengths, the
        cross-field ``correctIndex < len(options)``). The echoed object is what the tool step
        persists and what the response's ``quiz`` field carries.

        All-or-nothing (ADR-064 §5): ANY violation invalidates the WHOLE pool — one
        ``invalid_quiz`` tool-result error, the turn SURVIVES and the model regenerates the pool in
        the same turn. Partial acceptance (dropping the bad question) is forbidden: it would
        silently shrink the pool and hide the problem from the model. The message is CONTENT-FREE
        (field path + error kind only) so quiz text never leaks into the persisted tool-result echo.

        NOTE — the ``except`` branch below is DEFENSIVE, not the working path. On the live path the
        orchestrator validates args BEFORE dispatching here, and a failure there never reaches this
        handler: it is turned into the very same ``invalid_quiz`` tool-result by the degrade branch
        of ``_handle_tool_use`` (ADR-064 §5). This branch therefore only fires for direct calls to
        this handler (unit tests, a future caller that skips the orchestrator's validation), and it
        is kept so the contract holds for them too — the two producers are deliberately identical in
        code, message shape and behaviour.
        """
        try:
            validated = Quiz.model_validate(args)
        except ValidationError as exc:
            return ToolExecution.error(
                QUIZ_INVALID_ERROR_CODE,
                f"{content_free_args_error(exc)}; {QUIZ_CONSTRAINTS_HINT}",
            )
        return ToolExecution.ok(validated.model_dump())

    async def _media_generate(
        self,
        *,
        kind: str,
        args: dict[str, Any],
        user_id: uuid.UUID | None,
        turn_images: list[ImageAttachmentRef] | None = None,
        recent_image_urls: list[str] | None = None,
    ) -> ToolExecution:
        """Submit a media job (ADR-068). Never waits for fal completion.

        Maps service/domain errors to soft tool-result errors so the chat turn continues. Chat-turn
        billing is separate: media debit uses ``media-gen:{jobId}`` inside MediaGenerationService.
        """
        if self._media is None:
            return ToolExecution.error(
                MEDIA_NOT_CONFIGURED_ERROR_CODE,
                "media generation is not configured on this instance",
            )
        if user_id is None:
            return ToolExecution.error(
                MEDIA_NOT_CONFIGURED_ERROR_CODE,
                "media generation requires an authenticated user",
            )

        model_id = str(args.get("model") or "")
        prompt = str(args.get("prompt") or "")
        source_raw = args.get("sourceJobId")
        source_job_id: uuid.UUID | None = None
        if source_raw is not None:
            try:
                source_job_id = uuid.UUID(str(source_raw))
            except ValueError:
                return ToolExecution.error(MEDIA_INVALID_ERROR_CODE, "sourceJobId must be a UUID")

        if kind == "image":
            image_urls = list(args.get("imageUrls") or [])
            params = {
                "aspectRatio": args.get("aspectRatio"),
                "resolution": args.get("resolution"),
                "numImages": args.get("numImages"),
                "outputFormat": args.get("outputFormat"),
                "seed": args.get("seed"),
            }
        else:
            image_url = args.get("imageUrl")
            image_urls = [str(image_url)] if image_url else []
            params = {
                "negativePrompt": args.get("negativePrompt"),
                "aspectRatio": args.get("aspectRatio"),
                "resolution": args.get("resolution"),
                "duration": args.get("duration"),
                "generateAudio": args.get("generateAudio"),
                "cfgScale": args.get("cfgScale"),
                "seed": args.get("seed"),
            }

        use_recent = bool(args.get("useRecentImage"))
        # Same-turn chat photo → fal https when the model omitted an explicit reference.
        if source_job_id is None and not image_urls and turn_images:
            uploaded = await self._upload_turn_images(turn_images)
            if isinstance(uploaded, ToolExecution):
                return uploaded
            image_urls = uploaded
        elif source_job_id is None and not image_urls and use_recent:
            urls = [u for u in (recent_image_urls or []) if isinstance(u, str) and u]
            if not urls:
                return ToolExecution.error(
                    MEDIA_NO_RECENT_IMAGE_ERROR_CODE,
                    "no recent chat photo available (expired or missing); "
                    "ask the user to re-attach",
                )
            image_urls = urls

        try:
            view = await self._media.submit(
                user_id=user_id,
                kind=kind,
                model_id=model_id,
                prompt=prompt,
                image_urls=image_urls,
                params=params,
                source_job_id=source_job_id,
            )
        except MediaGenerationNotConfiguredError:
            return ToolExecution.error(
                MEDIA_NOT_CONFIGURED_ERROR_CODE,
                "media generation is not configured on this instance",
            )
        except InsufficientCreditsError:
            return ToolExecution.error(
                MEDIA_INSUFFICIENT_CREDITS_ERROR_CODE,
                "insufficient credits for media generation",
            )
        except NotFoundError:
            return ToolExecution.error(MEDIA_INVALID_ERROR_CODE, "sourceJobId not found")
        except ValidationFailedError as exc:
            # Catalog / enum / mutual-exclusion failures — content from our ValidationFailedError
            # messages (no user prompt echo beyond what the model already sent as args).
            detail = str(exc)[:400] or "invalid media request"
            return ToolExecution.error(MEDIA_INVALID_ERROR_CODE, detail)
        except UpstreamError:
            return ToolExecution.error(
                MEDIA_UPSTREAM_ERROR_CODE, "media provider unavailable; try again later"
            )

        job = view.job
        return ToolExecution.ok(
            {
                "jobId": str(job.id),
                "kind": job.kind,
                "status": job.status,
                "model": job.model_id,
                "creditsCharged": job.credits_charged,
            }
        )

    def _time_now(self, args: dict[str, Any]) -> ToolExecution:
        now_utc = self._clock.now()
        # Defensive: a Clock contract violation (naive/non-UTC) would corrupt the offsets; normalize
        # to UTC so utc/unix/weekday are always correct (ADR-026 §6 — UTC set independent of tz).
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=datetime.UTC)
        else:
            now_utc = now_utc.astimezone(datetime.UTC)

        result: dict[str, Any] = {
            "utc": now_utc.isoformat(),
            # Integer Unix timestamp in seconds (UTC), ADR-026 §6.
            "unix": int(now_utc.timestamp()),
            "weekday": _WEEKDAYS[now_utc.weekday()],
        }

        tz_raw = args.get("tz")
        if tz_raw is None:
            # No tz → UTC-only set (timezone/local omitted), ADR-026 §6.
            return ToolExecution.ok(result)

        tz_name = str(tz_raw)
        # Q-026-1: length cap (≤ 64) enforced here so an over-long tz degrades to invalid_timezone
        # (a tool-result error, the turn survives) rather than 422-ing the turn (ADR-026 §6).
        if len(tz_name) > TIME_NOW_TZ_MAX_LENGTH:
            return ToolExecution.error(
                "invalid_timezone",
                f"timezone name exceeds the {TIME_NOW_TZ_MAX_LENGTH}-character limit",
            )
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            # Unknown/unparseable IANA name, missing tz database in the image (TD-019), or a
            # filesystem-hostile name when a tz database IS present (ZoneInfo treats the name as a
            # path and the OS rejects it, e.g. OSError Errno 22) → invalid_timezone tool-result
            # error; the UTC set is still available, the turn survives (ADR-026 §6).
            return ToolExecution.error(
                "invalid_timezone", f"unknown or unavailable timezone: {tz_name}"
            )

        local_dt = now_utc.astimezone(zone)
        # Normalized IANA name (key(zone) is the canonical name passed to ZoneInfo), ADR-026 §6.
        result["timezone"] = str(zone.key)
        result["local"] = local_dt.isoformat()
        return ToolExecution.ok(result)
