"""Per-route authorization for the RU CloudPayments webhook (ADR-050 §1 / ADR-052,
billing-cloudpayments/03).

Isolated, constant-time secret check — modelled on ``require_adapty_webhook`` but using its own
secret ``CLOUDPAYMENTS_WEBHOOK_TOKEN`` (separate from JWT / admin / Adapty / KMS / preview, and
per-instance under ADR-017). Implemented as a per-route ``Depends``, NOT a global middleware: the
endpoint is fully isolated from the user JWT chain, the admin token and the Adapty webhook chain.

Under ADR-052 the header parsing is lenient: broadapps is a partner sender with a non-fixed
``Authorization`` format, so the raw header is read directly (``request.headers``) instead of
trusting FastAPI ``HTTPBearer`` extraction (which required exactly ``Bearer <token>`` and rejected
a valid raw secret). The ``HTTPBearer`` scheme is kept only as a decorative dependency so the
OpenAPI ``cloudPaymentsWebhook`` security scheme (lock icon / Authorize) is preserved; its
extracted credential is NOT used for verification.

Semantics:
- secret not configured (``CLOUDPAYMENTS_WEBHOOK_TOKEN == ""``) -> 500 (misconfiguration, clear
  text). A blank configured secret never matches any presented token, so the endpoint is active
  only where the secret is set. Checked BEFORE any extraction/compare.
- missing / mismatching token (any format) -> 401 (without revealing the reason) + one WARNING
  diagnostic log ``cloudpayments_webhook_auth_denied`` (safe allowlist).
The secret/token is never logged (only header names + the scheme word + ``matched``).
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api_gateway.openapi_security import cloudpayments_webhook_scheme
from app.config import get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError
from app.observability.logging import log_event

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.auth"

# Fixed allowlist of header NAMES (never values) surfaced on a 401 for diagnostics: lets us see if
# broadapps sends the secret in a different header / as a signature (ADR-052 §3, Q-052-1).
_AUTH_HEADER_ALLOWLIST = (
    "authorization",
    "x-api-key",
    "x-signature",
    "x-sign",
    "x-webhook-signature",
    "x-content-hmac",
    "content-hmac",
    "signature",
)


class CloudPaymentsWebhookMisconfiguredError(ServiceUnavailableError):
    """``CLOUDPAYMENTS_WEBHOOK_TOKEN`` is not configured — the endpoint cannot authenticate.

    Mapped to HTTP 500 (not 503): the contract (billing-cloudpayments/02-api-contracts.md)
    specifies a 500 misconfiguration response so the aggregator retries until the operator sets the
    secret. The clear message names the missing configuration without leaking any secret material.
    """

    status_code = 500
    code = "cloudpayments_webhook_misconfigured"


def _extract_webhook_credential(authorization: str | None) -> str | None:
    """Lenient extraction of the webhook credential from a raw ``Authorization`` (ADR-052 §1).

    - ``None`` / blank (after strip) -> ``None``.
    - ``Bearer <token>`` / ``Token <token>`` (scheme word case-insensitive) -> the value after the
      scheme (stripped); empty remainder -> ``None``.
    - a single part (no whitespace) -> the whole trimmed header (raw ``<token>`` without a scheme).
    - an unrecognised scheme (``Basic xxx``) -> the whole trimmed header as-is (won't match the
      secret -> 401, fail-closed; the scheme word is not stripped off).
    """
    if authorization is None:
        return None
    value = authorization.strip()
    if not value:
        return None
    parts = value.split(None, 1)  # split on the first run of whitespace
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
        rest = parts[1].strip()
        return rest or None
    return value


def _auth_scheme_label(authorization: str | None) -> str:
    """Return the scheme WORD only (never the token value) for the diagnostic log (ADR-052 §3).

    ``None`` -> ``"none"``; blank -> ``"empty"``; a single part (raw token, no whitespace) ->
    ``"raw"``; otherwise the first word in lower-case (``"bearer"``/``"token"``/``"basic"``/…) —
    safe because the token itself is in the second part.
    """
    if authorization is None:
        return "none"
    value = authorization.strip()
    if not value:
        return "empty"
    parts = value.split(None, 1)
    return parts[0].lower() if len(parts) == 2 else "raw"


def _log_auth_denied(request: Request) -> None:
    """Emit exactly one safe WARNING diagnostic on a 401 (ADR-052 §3).

    Strict allowlist: ``matched`` (always False here), ``authScheme`` (scheme word only) and
    ``presentAuthHeaders`` (NAMES of present headers from the fixed allowlist). NEVER the token
    value, the full ``Authorization`` header, any header value, or the body.
    """
    header = request.headers.get("authorization")
    present = [name for name in _AUTH_HEADER_ALLOWLIST if name in request.headers]
    log_event(
        logger,
        logging.WARNING,
        "cloudpayments_webhook_auth_denied",
        matched=False,
        authScheme=_auth_scheme_label(header),
        presentAuthHeaders=present,
    )


def require_cloudpayments_webhook(
    request: Request,
    _scheme: Annotated[
        HTTPAuthorizationCredentials | None, Depends(cloudpayments_webhook_scheme)
    ] = None,
) -> None:
    """Authorize a CloudPayments webhook call via the isolated static secret (ADR-050 §1 / ADR-052).

    Reads the raw ``Authorization`` header (``request.headers``) and extracts the credential
    leniently (``Bearer``/``Token`` prefix OR a raw token), so a valid secret from broadapps is
    accepted regardless of its wrapping. ``_scheme`` is a decorative ``SecurityBase`` dependency:
    it keeps the ``cloudPaymentsWebhook`` scheme (lock icon / Authorize) in OpenAPI but its
    extracted credential is NOT used — the real check reads the raw header.

    - unset secret -> 500 (before any extraction/compare).
    - mismatch / no header -> 401 (+ one WARNING diagnostic). Both the "no header" and the
      "wrong token" paths always run ``hmac.compare_digest`` (candidate defaults to ``""``), so
      there is no branch timing-leak between them. The secret/token is never logged.
    """
    secret = get_settings().cloudpayments_webhook_token
    if not secret:
        raise CloudPaymentsWebhookMisconfiguredError("cloudpayments webhook token not configured")
    candidate = _extract_webhook_credential(request.headers.get("authorization")) or ""
    matched = hmac.compare_digest(candidate, secret)
    if not matched:
        _log_auth_denied(request)
        raise UnauthorizedError("invalid cloudpayments webhook token")
