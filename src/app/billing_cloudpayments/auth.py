"""Per-route bearer authorization for the RU CloudPayments webhook (ADR-050 §1,
billing-cloudpayments/03).

Isolated, constant-time bearer check — modelled on ``require_adapty_webhook`` but using its own
secret ``CLOUDPAYMENTS_WEBHOOK_TOKEN`` (separate from JWT / admin / Adapty / KMS / preview, and
per-instance under ADR-017). Implemented as a per-route ``Depends``, NOT a global middleware: the
endpoint is fully isolated from the user JWT chain, the admin token and the Adapty webhook chain.

Semantics:
- secret not configured (``CLOUDPAYMENTS_WEBHOOK_TOKEN == ""``) -> 500 (misconfiguration, clear
  text). A blank configured secret never matches any presented token, so the endpoint is active
  only where the secret is set.
- missing / non-Bearer / mismatching token -> 401 (without revealing the reason).
The secret is never logged (the ``authorization`` header is covered by the redaction denylist).
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.api_gateway.openapi_security import cloudpayments_webhook_scheme
from app.config import get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError


class CloudPaymentsWebhookMisconfiguredError(ServiceUnavailableError):
    """``CLOUDPAYMENTS_WEBHOOK_TOKEN`` is not configured — the endpoint cannot authenticate.

    Mapped to HTTP 500 (not 503): the contract (billing-cloudpayments/02-api-contracts.md)
    specifies a 500 misconfiguration response so the aggregator retries until the operator sets the
    secret. The clear message names the missing configuration without leaking any secret material.
    """

    status_code = 500
    code = "cloudpayments_webhook_misconfigured"


def require_cloudpayments_webhook(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(cloudpayments_webhook_scheme)
    ] = None,
) -> None:
    """Authorize a CloudPayments webhook call via the isolated static bearer secret (ADR-050 §1).

    The token value comes from ``cloudpayments_webhook_scheme`` (HTTPBearer, ``auto_error=False``):
    a ``SecurityBase`` that contributes the ``cloudPaymentsWebhook`` scheme to OpenAPI (Authorize)
    without raising on a missing/malformed header, so the 500-on-unset / 401-on-mismatch behaviour
    below is the single source of truth. Comparison is constant-time (``hmac.compare_digest``).
    """
    secret = get_settings().cloudpayments_webhook_token
    if not secret:
        raise CloudPaymentsWebhookMisconfiguredError("cloudpayments webhook token not configured")
    presented = credentials.credentials if credentials is not None else None
    if presented is None or not hmac.compare_digest(presented, secret):
        raise UnauthorizedError("invalid cloudpayments webhook token")
