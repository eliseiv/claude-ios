"""Proxy-callback token, callback URL and callback parsing (ADR-108 §4).

The token is ``hex(HMAC-SHA256(webhook_secret, str(jobId)))`` — deterministic by job id, never
stored. ``webhook_secret`` is ``PROXY_WEBHOOK_SECRET`` if set, otherwise ``PROXY_API_KEY``. It is a
DIFFERENT token from the signed asset-download token (ADR-085, ``PREVIEW_URL_SECRET``, in the
PATH, handed to the client): this one lives in the QUERY, is never handed to a client and opens
exactly one action — applying an outcome to job ``jobId``.

Nothing here logs the secret, the token, the callback body or an asset URL.
"""

from __future__ import annotations

import contextlib
import decimal
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any

from app.config import Settings
from app.errors import MediaGenerationNotConfiguredError
from app.observability.logging import PROXY_WEBHOOK_PATH_PREFIX, log_event

logger = logging.getLogger(__name__)  # == "app.media_generation.webhook"

WEBHOOK_PATH_PREFIX = PROXY_WEBHOOK_PATH_PREFIX
TOKEN_MAX_LENGTH = 128

OUTCOME_PENDING = "pending"
OUTCOME_FAILED = "failed"
OUTCOME_COMPLETED = "completed"

# ADR-108 §10: values of `media_webhook_outcome.outcome`, each chosen by a predicate over the
# facts of the request (mutually exclusive):
WEBHOOK_BAD_TOKEN = "bad_token"  # §4.2 п.1 — token length/HMAC mismatch, DB not touched
WEBHOOK_NOT_JSON = "not_json"  # §4.2 п.2 — body is not a JSON object
WEBHOOK_UNKNOWN_JOB = "unknown_job"  # §4.2 п.3 — no row, or a legacy row (provider = '')
WEBHOOK_DUPLICATE_TERMINAL = "duplicate_terminal"  # §4.2 п.4 — job already terminal
WEBHOOK_PENDING = "pending"  # §4.3 — callback classified pending → running
WEBHOOK_FAILED = "failed"  # §4.3 — callback classified failed → _fail (refund)
WEBHOOK_COMPLETED = "completed"  # §5 ran and the job IS `completed` (assets handed out)
WEBHOOK_COMPLETION_FAILED = "completion_failed"  # §5 ran and the job IS `failed` (blocked / 422)
WEBHOOK_COMPLETION_DEFERRED = "completion_deferred"  # pending_result kept, §5 hit a transient fault
WEBHOOK_NO_USABLE_ASSET = "no_usable_asset"  # §4.3 п.2 dropped every asset → failed
WEBHOOK_RESULT_ALREADY_RECEIVED = "result_already_received"  # §4.3 step 0 ignored failed/pending

_WARNING_OUTCOMES = frozenset({WEBHOOK_BAD_TOKEN, WEBHOOK_UNKNOWN_JOB, WEBHOOK_NO_USABLE_ASSET})

_TERMINAL_FAIL = frozenset({"failed", "fail", "error", "canceled", "cancelled", "moderated"})
_TERMINAL_OK = frozenset({"completed", "success", "succeeded", "ok"})
_FAILURE_CODES = (400, 500, 501)

# NUMERIC(18,6): at most 12 integer digits.
_VENDOR_PRICE_LIMIT = decimal.Decimal(10) ** 12


def webhook_secret(settings: Settings) -> str:
    """Signing secret for ``callbackUrl``: dedicated secret, else the instance proxy key."""
    dedicated = settings.proxy_webhook_secret.strip()
    if dedicated:
        return dedicated
    return settings.proxy_api_key.strip()


def sign_webhook_token(*, settings: Settings, job_id: uuid.UUID) -> str:
    secret = webhook_secret(settings)
    digest = hmac.new(secret.encode("utf-8"), str(job_id).encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def verify_webhook_token(*, settings: Settings, job_id: uuid.UUID, token: str | None) -> bool:
    """Constant-time check; an empty secret never verifies (ADR-108 §4.2 п.1)."""
    if not token or len(token) > TOKEN_MAX_LENGTH:
        return False
    if not webhook_secret(settings):
        return False
    expected = sign_webhook_token(settings=settings, job_id=job_id)
    return hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8"))


def callback_url(*, settings: Settings, job_id: uuid.UUID) -> str:
    """Absolute URL the proxy POSTs to when the vendor finishes (ADR-108 §4.1).

    Unlike the sample repository there is no ``localhost`` fallback: ``proxy_configured`` already
    requires ``SERVICE_DOMAIN``, and a job without a reachable callback must not be debited.
    """
    host = settings.normalized_service_domain()
    if not host:
        raise MediaGenerationNotConfiguredError("media generation is not configured")
    token = sign_webhook_token(settings=settings, job_id=job_id)
    return f"https://{host}{WEBHOOK_PATH_PREFIX}/{job_id}?token={token}"


def log_webhook_outcome(*, job_id: str | None, proxy_service: str | None, outcome: str) -> None:
    """``media_webhook_outcome`` (ADR-108 §10). No token, no body, no URL."""
    level = logging.WARNING if outcome in _WARNING_OUTCOMES else logging.INFO
    log_event(
        logger,
        level,
        "media_webhook_outcome",
        jobId=job_id,
        proxyService=proxy_service or None,
        outcome=outcome,
    )


def webhook_outcome(body: dict[str, Any]) -> str:
    """Classify a callback as ``failed``, ``completed`` or ``pending`` (the sample's rule)."""
    if webhook_failed(body):
        return OUTCOME_FAILED
    if _status_token(body) in _TERMINAL_OK:
        return OUTCOME_COMPLETED
    if collect_urls(body):
        return OUTCOME_COMPLETED
    return OUTCOME_PENDING


def webhook_failed(body: dict[str, Any]) -> bool:
    status = _status_token(body)
    if status in _TERMINAL_FAIL:
        return True
    if status in _TERMINAL_OK:
        return False
    code = body.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code in _FAILURE_CODES:
        return True
    return body.get("error") is True


def webhook_error_message(body: dict[str, Any]) -> str:
    """The vendor's failure text (≤ 500 chars, one line) or a generic one."""
    return _error_message(body) or "generation failed upstream"


def parse_vendor_price(body: dict[str, Any]) -> decimal.Decimal | None:
    """Actual vendor cost of the run, if the callback reports one (ADR-108 §8)."""
    for key in ("vendor_price", "vendorPrice", "cost"):
        parsed = _as_decimal(body.get(key))
        if parsed is not None:
            return parsed
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("vendor_price", "vendorPrice", "cost", "creditsConsumed"):
            parsed = _as_decimal(data.get(key))
            if parsed is not None:
                return parsed
    return None


def fal_shaped_candidates(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Dicts of the callback that may carry fal's output form: top level, payload/data/result."""
    candidates = [body]
    for key in ("payload", "data", "result"):
        value = body.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    return candidates


def collect_urls(body: Any, *, _depth: int = 0) -> list[str]:
    """Every https asset URL the callback names, unique and in stable order (sample rule)."""
    if _depth > 6 or body is None:
        return []
    found: list[str] = []
    if isinstance(body, str):
        if body.startswith("https://") and _looks_like_asset(body):
            found.append(body)
        elif body.startswith(("{", "[")):
            with contextlib.suppress(ValueError, TypeError):
                found.extend(collect_urls(json.loads(body), _depth=_depth + 1))
        return found
    if isinstance(body, list):
        for item in body:
            found.extend(collect_urls(item, _depth=_depth + 1))
        return _unique(found)
    if not isinstance(body, dict):
        return []
    for key in (
        "result_file_url",
        "resultFileUrl",
        "resultImageUrl",
        "url",
        "mediaUrl",
        "output_url",
    ):
        value = body.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            found.append(value)
    for key in ("resultUrls", "output_urls", "image_urls", "images", "videos", "assets"):
        found.extend(collect_urls(body.get(key), _depth=_depth + 1))
    for key in ("data", "payload", "result", "video", "image", "info", "resultJson"):
        found.extend(collect_urls(body.get(key), _depth=_depth + 1))
    return _unique(found)


def _unique(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _status_token(body: dict[str, Any]) -> str:
    for source in (body, body.get("data")):
        if not isinstance(source, dict):
            continue
        for key in ("status", "state"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value.strip().lower()
    return ""


def _error_message(body: dict[str, Any]) -> str | None:
    for key in ("error", "failMsg", "message", "msg"):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if value.strip().lower() in {"error", "true"}:
            continue
        return " ".join(value.split())[:500]
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("error", "failMsg", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())[:500]
    return None


def _as_decimal(raw: Any) -> decimal.Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int | float):
        candidate: decimal.Decimal | None = decimal.Decimal(str(raw))
    elif isinstance(raw, str) and raw.strip():
        try:
            candidate = decimal.Decimal(raw.strip())
        except decimal.InvalidOperation:
            return None
    else:
        return None
    # NaN/Infinity and values outside NUMERIC(18,6) would fail the whole callback transaction;
    # the price is diagnostic, so an unusable value is simply not recorded.
    if candidate is None or not candidate.is_finite() or candidate < 0:
        return None
    if candidate >= _VENDOR_PRICE_LIMIT:
        return None
    return candidate.quantize(decimal.Decimal("0.000001"), rounding=decimal.ROUND_HALF_UP)


def _looks_like_asset(url: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered
        for token in (
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".mp4",
            "/files/",
            "fal.media",
            "sosana.",
            "aiquickdraw",
            "storage.",
        )
    )
