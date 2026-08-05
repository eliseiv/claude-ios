"""Outgoing calls to the fal.ai queue API — submit / status / result (ADR-060 §3).

Every generation goes through the *queue* API (``https://queue.fal.run``) rather than the
synchronous one: Kling and Veo runs take minutes, far beyond any sane HTTP timeout, so the
submit call returns a ``request_id`` immediately and the client polls ``GET /v1/media/jobs/{id}``.
Image models are fast but use the same path — one code path, one job lifecycle.

The API key is presented as ``Authorization: Key <FAL_API_KEY>`` (fal's own scheme, not Bearer)
and is never logged: ``log_event`` redacts ``*key*`` fields, and we do not put it in a field at
all. Upstream bodies are not proxied outward except for the ``422`` case, where fal's validation
message names the offending model parameter and is genuinely useful to the client.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.errors import (
    MediaGenerationNotConfiguredError,
    RateLimitedError,
    UpstreamError,
    ValidationFailedError,
)
from app.observability.logging import log_event

logger = logging.getLogger(__name__)  # == "app.media_generation.fal_client"

# fal queue statuses (docs: model-apis/queue). Mapped to our job statuses in the service.
FAL_IN_QUEUE = "IN_QUEUE"
FAL_IN_PROGRESS = "IN_PROGRESS"
FAL_COMPLETED = "COMPLETED"
FAL_FAILED = "FAILED"
FAL_CANCELED = "CANCELED"


@dataclass(frozen=True)
class FalSubmission:
    """Accepted queue submission: the handle we persist to poll the run later."""

    request_id: str
    status: str
    status_url: str
    response_url: str
    queue_position: int | None


@dataclass(frozen=True)
class FalStatus:
    """Current queue state of a submitted request."""

    status: str
    queue_position: int | None
    error: str | None


class FalClient:
    """Thin HTTP client over the fal queue API. No DB, no persisted state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._settings.fal_api_key)

    def _headers(self) -> dict[str, str]:
        if not self._settings.fal_api_key:
            raise MediaGenerationNotConfiguredError("media generation is not configured")
        return {
            "Authorization": f"Key {self._settings.fal_api_key}",
            "Accept": "application/json",
        }

    def _lifecycle_header(self) -> dict[str, str]:
        """Per-request retention preference for the files this run produces (ADR-061 §5).

        fal keeps generated assets "at least 7 days" and then deletes them permanently; since we
        hand the CDN URL straight to the client and store it in ``media_jobs.result``, that default
        outlives neither a job list nor a user's memory of it. Sent only on submit — the object is
        created there — and omitted entirely when the operator has expressed no preference.
        """
        preference = self._settings.fal_asset_retention()
        if preference is False:
            return {}
        return {
            "X-Fal-Object-Lifecycle-Preference": json.dumps(
                {"expiration_duration_seconds": preference}
            )
        }

    def _queue_base(self) -> str:
        return self._settings.fal_queue_base.rstrip("/")

    def _trusted(self, url: str) -> bool:
        """Guard against following an unexpected host from an upstream-supplied URL."""
        return url.startswith(f"{self._queue_base()}/")

    async def submit(self, *, endpoint: str, payload: dict[str, Any]) -> FalSubmission:
        """Enqueue a generation run and return its polling handle."""
        url = f"{self._queue_base()}/{endpoint}"
        body = await self._request(
            "POST", url, endpoint=endpoint, json=payload, extra_headers=self._lifecycle_header()
        )

        request_id = body.get("request_id") if isinstance(body, dict) else None
        if not isinstance(request_id, str) or not request_id:
            raise self._upstream_error("malformed_submit", endpoint=endpoint)

        # fal returns ready-made polling URLs. Prefer them (they already encode the app-level
        # path, which for nested endpoints such as kling-video/v3/pro/... is not derivable from
        # the endpoint id alone), but fall back to the canonical shape if they look wrong.
        status_url = body.get("status_url")
        response_url = body.get("response_url")
        fallback = f"{self._queue_base()}/{endpoint}/requests/{request_id}"
        if not isinstance(status_url, str) or not self._trusted(status_url):
            status_url = f"{fallback}/status"
        if not isinstance(response_url, str) or not self._trusted(response_url):
            response_url = fallback

        position = body.get("queue_position")
        status = body.get("status")
        log_event(
            logger,
            logging.INFO,
            "fal_submit_outcome",
            result="queued",
            falEndpoint=endpoint,
            falRequestId=request_id,
            status=status if isinstance(status, str) else None,
        )
        return FalSubmission(
            request_id=request_id,
            status=status if isinstance(status, str) else FAL_IN_QUEUE,
            status_url=status_url,
            response_url=response_url,
            queue_position=position if isinstance(position, int) else None,
        )

    async def status(self, *, status_url: str, endpoint: str) -> FalStatus:
        """Poll the queue state of a previously submitted request."""
        if not self._trusted(status_url):
            raise self._upstream_error("untrusted_status_url", endpoint=endpoint)
        body = await self._request("GET", status_url, endpoint=endpoint)
        status = body.get("status") if isinstance(body, dict) else None
        position = body.get("queue_position") if isinstance(body, dict) else None
        error = body.get("error") if isinstance(body, dict) else None
        return FalStatus(
            status=status if isinstance(status, str) else FAL_IN_PROGRESS,
            queue_position=position if isinstance(position, int) else None,
            error=error if isinstance(error, str) else None,
        )

    async def result(self, *, response_url: str, endpoint: str) -> dict[str, Any]:
        """Fetch the model output of a COMPLETED request."""
        if not self._trusted(response_url):
            raise self._upstream_error("untrusted_response_url", endpoint=endpoint)
        body = await self._request("GET", response_url, endpoint=endpoint)
        if not isinstance(body, dict):
            raise self._upstream_error("malformed_result", endpoint=endpoint)
        return body

    async def _request(
        self,
        method: str,
        url: str,
        *,
        endpoint: str,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        timeout = self._settings.fal_timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(method, url, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise self._upstream_error("timeout", endpoint=endpoint) from exc
        except httpx.RequestError as exc:
            raise self._upstream_error("connect_error", endpoint=endpoint) from exc

        self._raise_for_status(response, endpoint=endpoint)
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._upstream_error("malformed_response", endpoint=endpoint) from exc

    def _raise_for_status(self, response: httpx.Response, *, endpoint: str) -> None:
        code = response.status_code
        if 200 <= code < 300:
            return
        if code in (401, 403):
            # A rejected key is an operator problem, not a client one: surface it as
            # "not configured" (503) so it is distinguishable from a fal outage (502).
            log_event(
                logger,
                logging.ERROR,
                "fal_call_outcome",
                result="error",
                reason="unauthorized",
                falEndpoint=endpoint,
                upstreamStatus=code,
            )
            raise MediaGenerationNotConfiguredError("media generation provider rejected the key")
        if code == 422:
            detail = _validation_detail(response)
            log_event(
                logger,
                logging.WARNING,
                "fal_call_outcome",
                result="error",
                reason="upstream_validation",
                falEndpoint=endpoint,
            )
            raise ValidationFailedError(detail)
        if code == 429:
            log_event(
                logger,
                logging.WARNING,
                "fal_call_outcome",
                result="error",
                reason="upstream_rate_limited",
                falEndpoint=endpoint,
            )
            raise RateLimitedError("generation provider rate limit exceeded")
        reason = "upstream_payment_required" if code == 402 else "upstream_status"
        raise self._upstream_error(reason, endpoint=endpoint, upstream_status=code)

    def _upstream_error(
        self, reason: str, *, endpoint: str, upstream_status: int | None = None
    ) -> UpstreamError:
        log_event(
            logger,
            logging.WARNING,
            "fal_call_outcome",
            result="error",
            reason=reason,
            falEndpoint=endpoint,
            upstreamStatus=upstream_status,
        )
        return UpstreamError("generation provider unavailable")


def _validation_detail(response: httpx.Response) -> str:
    """Extract a short, safe message from a fal 422 body.

    fal validation errors name the offending model parameter, which is exactly what the client
    needs to fix the request, and contain no credentials. The text is truncated and flattened to
    a single line so it cannot bloat or break our error envelope.
    """
    fallback = "generation provider rejected the request parameters"
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        return fallback
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        text = detail
    elif isinstance(detail, list) and detail:
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            loc = item.get("loc")
            msg = item.get("msg")
            where = ".".join(str(p) for p in loc) if isinstance(loc, list) else None
            if isinstance(msg, str):
                parts.append(f"{where}: {msg}" if where else msg)
        text = "; ".join(parts) if parts else fallback
    else:
        return fallback
    return " ".join(text.split())[:500]
