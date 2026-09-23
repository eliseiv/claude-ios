"""Incoming proxy-service callbacks for media jobs: ``POST /v1/media/webhooks/proxy/{jobId}``.

ADR-108 §4.2. Server-to-server (the proxy calls it by the ``callbackUrl`` we passed at submit);
the iOS client never does. Hence:

* its own router — NOT under ``require_media_generation_configured`` (a job in flight must still
  get its outcome after ``PROXY_API_KEY`` is removed for a rollback) and not under the per-user
  rate limit of ``/v1/media/*``; no JWT;
* ``include_in_schema=False`` — not a client contract, the public Swagger stays as it was;
* the body limit is the general ``SIZE_LIMIT_BODY``; no rate limit (one source, and a refused
  request costs one HMAC before any DB access).

Order of checks (normative): (1) ``token`` missing / longer than 128 / HMAC mismatch (empty secret
⇒ always a mismatch) → ``401`` WITHOUT touching the DB; (2) body not a JSON object → ``422``;
(3) no job or a legacy job → ``404``; (4) terminal → ``200`` unchanged; (5) apply → ``200``.

Never logged: the token, the secret, the callback body, an asset URL.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.config import get_settings
from app.deps import get_media_generation_service
from app.errors import UnauthorizedError, ValidationFailedError
from app.media_generation.service import MediaGenerationService
from app.media_generation.webhook import (
    WEBHOOK_BAD_TOKEN,
    WEBHOOK_NOT_JSON,
    log_webhook_outcome,
    verify_webhook_token,
)

router = APIRouter(prefix="/v1/media/webhooks", include_in_schema=False)


def _parse_job_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


@router.post("/proxy/{job_id}")
async def proxy_media_webhook(
    job_id: str,
    request: Request,
    media: Annotated[MediaGenerationService, Depends(get_media_generation_service)],
) -> dict[str, Any]:
    # (1) Authentication first, from the query alone: no DB statement runs before it. The path is
    # taken as a plain string so that a malformed id is a 401 (no token can match it), keeping
    # "401 before anything else" true for every request to this route.
    token = request.query_params.get("token")
    parsed_id = _parse_job_id(job_id)
    if parsed_id is None or not verify_webhook_token(
        settings=get_settings(), job_id=parsed_id, token=token
    ):
        log_webhook_outcome(
            job_id=None if parsed_id is None else str(parsed_id),
            proxy_service=None,
            outcome=WEBHOOK_BAD_TOKEN,
        )
        raise UnauthorizedError("invalid webhook token")

    # (2) The body must be a JSON object.
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        log_webhook_outcome(job_id=str(parsed_id), proxy_service=None, outcome=WEBHOOK_NOT_JSON)
        raise ValidationFailedError("webhook body must be a JSON object")

    # (3)–(5) in the service, under the row lock, in the request transaction.
    await media.handle_proxy_webhook(job_id=parsed_id, body=body)
    return {"ok": True}
