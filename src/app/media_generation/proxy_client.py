"""Outgoing client of the proxy service: ``POST {PROXY_BASE}/api/v1/tasks`` (ADR-108 §3).

We authenticate as the instance with ``Authorization: Bearer <PROXY_API_KEY>`` and supply our
``callbackUrl``. A kie / sosana task also carries the instance provider key as ``apiKey``
(``KIE_API_KEY`` / ``SOSANA_API_KEY``, ADR-108 §3.2); an empty key is not sent and the proxy
generates with its own. No key and no callback token is logged or stored, and a vendor text that
reaches the client or ``media_jobs.error`` is masked first (``mask_secret_text``, §10).

Error mapping (ADR-108 §3.3) is expressed through the exception CLASS, and the route loop of the
service decides what to do with it:

* timeout / connect → ``ProxyTransportError`` (an ``UpstreamError``, ``502``): the loop STOPS —
  every route goes through the same proxy host, and after a timeout the proxy may have accepted
  the task, so trying the next route could pay for a second run;
* malformed JSON / ``2xx`` with ``error: true`` / ``5xx`` / ``402`` / ``400`` without a validation
  marker → ``UpstreamError``: next route;
* ``429`` → ``RateLimitedError``: next route;
* ``401`` / ``403`` → ``MediaGenerationNotConfiguredError``: stop on the fal route, next route on
  sosana/kie;
* ``422`` or ``400`` with a validation marker → ``ValidationFailedError``: stop on the fal route,
  next route on sosana/kie (the service decides by the route).
"""

from __future__ import annotations

import logging
import re
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
from app.media_generation.fal_client import _validation_detail
from app.media_generation.routing import SERVICE_KIE, SERVICE_SOSANA
from app.observability.logging import log_event

logger = logging.getLogger(__name__)  # == "app.media_generation.proxy_client"

_ID_KEYS = ("request_id", "requestId", "id", "uid", "taskId")
_REJECTED_FALLBACK = "generation provider rejected the request"
_MASK = "***"
# Credentials a vendor might echo back inside free text: bearer keys, sk_ keys, key=/token=/secret=.
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[^\s\"',]+|\bsk_[A-Za-z0-9_-]+|((?:token|key|secret)=)[^&\s\"',]+"
)


def mask_secret_text(text: str, *, settings: Settings) -> str:
    """Vendor text with our live keys and key-shaped fragments replaced by ``***`` (ADR-108 §10).

    The vendor may echo the request back, ``apiKey`` included; the text then reaches the client,
    ``media_jobs.error`` and the logs.
    """
    for secret in (settings.kie_api_key, settings.sosana_api_key, settings.proxy_api_key):
        value = secret.strip()
        if value:
            text = text.replace(value, _MASK)
    return _SECRET_TEXT.sub(lambda m: f"{m.group(1) or m.group(2) or ''}{_MASK}", text)


class ProxyTransportError(UpstreamError):
    """The proxy itself did not answer (timeout / connect): ``502``, no fallback (ADR-108 §3.3)."""


@dataclass(frozen=True)
class ProxySubmission:
    """Accepted proxy task. ``request_id`` is diagnostic only — the callback is keyed by jobId."""

    request_id: str


class ProxyClient:
    """Thin httpx client over ``POST /api/v1/tasks``. No DB, no persisted state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        # ADR-108 §1: the one predicate (key AND service domain), not the raw key.
        return self._settings.proxy_configured()

    def _headers(self) -> dict[str, str]:
        if not self.configured:
            raise MediaGenerationNotConfiguredError("media generation is not configured")
        return {
            "Authorization": f"Bearer {self._settings.proxy_api_key.strip()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _vendor_api_key(self, service: str) -> str:
        if service == SERVICE_KIE:
            return self._settings.kie_api_key.strip()
        if service == SERVICE_SOSANA:
            return self._settings.sosana_api_key.strip()
        return ""

    def _tasks_url(self) -> str:
        return f"{self._settings.proxy_base.rstrip('/')}/api/v1/tasks"

    async def submit(
        self,
        *,
        service: str,
        endpoint: str,
        payload: dict[str, Any],
        callback_url: str,
        catalog_endpoint: str,
    ) -> ProxySubmission:
        body: dict[str, Any] = {
            "service": service,
            "endpoint": endpoint,
            "method": "POST",
            "payload": payload,
            "callbackUrl": callback_url,
        }
        vendor_key = self._vendor_api_key(service)
        if vendor_key:
            body["apiKey"] = vendor_key
        response = await self._request(body, endpoint=catalog_endpoint)
        request_id = _first_id(response)
        log_event(
            logger,
            logging.INFO,
            "proxy_submit_outcome",
            result="queued",
            proxyService=service,
            falEndpoint=catalog_endpoint,
            proxyRequestId=request_id or None,
        )
        return ProxySubmission(request_id=request_id)

    async def _request(self, body: dict[str, Any], *, endpoint: str) -> dict[str, Any]:
        headers = self._headers()
        timeout = self._settings.proxy_timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    "POST", self._tasks_url(), headers=headers, json=body
                )
        except httpx.TimeoutException as exc:
            raise self._transport_error("timeout", endpoint=endpoint) from exc
        except httpx.RequestError as exc:
            raise self._transport_error("connect_error", endpoint=endpoint) from exc

        self._raise_for_status(response, endpoint=endpoint)
        try:
            parsed = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._upstream_error("malformed_response", endpoint=endpoint) from exc
        if not isinstance(parsed, dict):
            raise self._upstream_error("malformed_response", endpoint=endpoint)
        if parsed.get("error") is True:
            raise self._upstream_error(
                "upstream_status", endpoint=endpoint, upstream_status=response.status_code
            )
        return parsed

    def _raise_for_status(self, response: httpx.Response, *, endpoint: str) -> None:
        code = response.status_code
        if 200 <= code < 300:
            return
        if code in (401, 403):
            log_event(
                logger,
                logging.ERROR,
                "proxy_call_outcome",
                result="error",
                reason="unauthorized",
                falEndpoint=endpoint,
                upstreamStatus=code,
            )
            raise MediaGenerationNotConfiguredError("media generation provider rejected the key")
        if code == 422:
            detail = mask_secret_text(_validation_detail(response), settings=self._settings)
            self._log_validation(endpoint=endpoint, code=code)
            raise ValidationFailedError(detail)
        if code == 429:
            log_event(
                logger,
                logging.WARNING,
                "proxy_call_outcome",
                result="error",
                reason="upstream_rate_limited",
                falEndpoint=endpoint,
                upstreamStatus=code,
            )
            raise RateLimitedError("generation provider rate limit exceeded")
        if code == 400:
            # The proxy wraps vendor rejections as 400 {error, message}; only a message that names
            # a validation problem is treated as one.
            detail = mask_secret_text(_proxy_error_message(response), settings=self._settings)
            if _looks_like_validation(detail):
                self._log_validation(endpoint=endpoint, code=code)
                raise ValidationFailedError(detail)
            raise self._upstream_error("upstream_status", endpoint=endpoint, upstream_status=code)
        reason = "upstream_payment_required" if code == 402 else "upstream_status"
        raise self._upstream_error(reason, endpoint=endpoint, upstream_status=code)

    @staticmethod
    def _log_validation(*, endpoint: str, code: int) -> None:
        log_event(
            logger,
            logging.WARNING,
            "proxy_call_outcome",
            result="error",
            reason="upstream_validation",
            falEndpoint=endpoint,
            upstreamStatus=code,
        )

    @staticmethod
    def _upstream_error(
        reason: str, *, endpoint: str, upstream_status: int | None = None
    ) -> UpstreamError:
        log_event(
            logger,
            logging.WARNING,
            "proxy_call_outcome",
            result="error",
            reason=reason,
            falEndpoint=endpoint,
            upstreamStatus=upstream_status,
        )
        return UpstreamError("generation provider unavailable")

    @staticmethod
    def _transport_error(reason: str, *, endpoint: str) -> ProxyTransportError:
        log_event(
            logger,
            logging.WARNING,
            "proxy_call_outcome",
            result="error",
            reason=reason,
            falEndpoint=endpoint,
            upstreamStatus=None,
        )
        return ProxyTransportError("generation provider unavailable")


def _first_id(body: dict[str, Any], *, _depth: int = 0) -> str:
    """First non-empty id of the proxy answer, also inside ``data``; ``""`` when there is none.

    ADR-108 §3.3: an empty string, not the sample's ``"pending"`` — that reads like a status and
    would be the same value for every such job.
    """
    for key in _ID_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    data = body.get("data")
    if isinstance(data, dict) and _depth < 3:
        return _first_id(data, _depth=_depth + 1)
    return ""


def _proxy_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        return _REJECTED_FALLBACK
    if not isinstance(body, dict):
        return _REJECTED_FALLBACK
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        return " ".join(message.split())[:500]
    return _REJECTED_FALLBACK


def _looks_like_validation(message: str) -> bool:
    lowered = message.lower()
    return "validation" in lowered or "unprocessable" in lowered
