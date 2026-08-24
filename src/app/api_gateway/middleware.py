"""Gateway middleware: size limit, correlation id, security headers (api-gateway/03)."""

from __future__ import annotations

import json
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.observability.context import set_request_id, set_session_id, set_user_id


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generates/propagates X-Request-Id (HTTP correlation id, NOT a billing key, ADR-005)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        set_request_id(request_id)
        set_session_id(None)
        set_user_id(None)
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


class SizeLimitMiddleware:
    """Rejects bodies exceeding the route limit with a real 413 (ADR-089 §2, closes TD-017).

    Pure ASGI (not BaseHTTPMiddleware) for two reasons the header-based guard could not satisfy:
      - it counts ACTUALLY READ bytes, so a chunked request without ``Content-Length`` is rejected
        too (the old guard silently skipped the check when the header was absent — TD-017);
      - it DRAINS the rest of the body (bounded by ``SIZE_LIMIT_DRAIN_BYTES``) before answering, so
        the client finishes writing and gets to READ the response. Closing the socket mid-upload is
        exactly the broken pipe iOS showed the user as «нет связи» instead of «файл слишком большой»
        (QA report BUG-004). The drain budget is bounded on purpose: an unbounded drain would defeat
        the limit it protects.

    The body is buffered here (bounded BY the limit itself, so at most `limit` bytes) and replayed
    to the app — nothing downstream changes.

    The general limit applies to all routes. Some routes get a RAISED transport limit because they
    accept large base64 payloads; each raise is scoped to its own route so the attack surface is not
    widened globally. The path set is a DERIVATIVE of the invariant «every route whose body may
    carry attachments[]» (ADR-089 §1) — maintaining it by hand already produced a defect
    (/v1/chat/v2/run/stream takes the same ChatV2RunRequest but was missing):
      - /v1/chat/run, /v1/chat/v2/run, /v1/chat/v2/run/stream — inline base64 attachments (ADR-020);
      - POST /v1/workspaces/{id}/files — base64 workspace knowledge-file upload (ADR-045). Matched
        by path prefix+suffix (the path carries the workspace id), method-agnostic like the
        chat run rule: GET /v1/workspaces/{id}/files also matches but carries no body, so the
        raised limit is harmless for it.
      - POST /v1/media/uploads — base64 reference image for image-to-image / image-to-video
        (ADR-062). Exact path, so nothing else under /v1/media/* is widened.
      - POST /v1/admin/media/templates — base64 gallery cover (ADR-066). Exact path.
    """

    _CHAT_RUN_PATHS = frozenset({"/v1/chat/run", "/v1/chat/v2/run", "/v1/chat/v2/run/stream"})
    _WORKSPACES_PREFIX = "/v1/workspaces/"
    _FILES_SUFFIX = "/files"
    _MEDIA_UPLOAD_PATH = "/v1/media/uploads"
    _MEDIA_TEMPLATE_CREATE_PATH = "/v1/admin/media/templates"

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        settings = get_settings()
        self._limit = settings.size_limit_body
        self._chat_run_limit = settings.attachment_request_body_limit
        self._workspace_files_limit = settings.workspace_request_body_limit
        self._media_upload_limit = settings.media_upload_request_body_limit
        self._media_template_cover_limit = settings.media_template_cover_request_body_limit
        self._drain_budget = settings.size_limit_drain_bytes

    def _limit_for(self, path: str) -> int:
        if path in self._CHAT_RUN_PATHS:
            return self._chat_run_limit
        if path == self._MEDIA_UPLOAD_PATH:
            return self._media_upload_limit
        if path == self._MEDIA_TEMPLATE_CREATE_PATH:
            return self._media_template_cover_limit
        if path.startswith(self._WORKSPACES_PREFIX) and path.endswith(self._FILES_SUFFIX):
            return self._workspace_files_limit
        return self._limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limit = self._limit_for(scope.get("path", ""))
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > limit:
                # Trust the header for the early reject, but still drain so the client can read us.
                await self._too_large(headers, limit, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > limit:
                await self._too_large(headers, limit, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self._app(scope, replay, send)

    async def _too_large(
        self, headers: dict[bytes, bytes], limit: int, receive: Receive, send: Send
    ) -> None:
        """Drain what the client is still sending (bounded), then answer a real 413."""
        drained = 0
        while drained < self._drain_budget:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            drained += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        request_id = headers.get(b"x-request-id", b"").decode("latin-1") or str(uuid.uuid4())
        payload = json.dumps(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"request body exceeds the {limit} byte limit for this route",
                    "requestId": request_id,
                }
            },
            ensure_ascii=False,
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"connection", b"close"),
                    (b"x-request-id", request_id.encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Default API security headers.

    The preview endpoint (/v1/preview/*) serves user (Claude-generated) HTML/JS and needs its own
    sandbox headers (CSP sandbox, X-Frame-Options: SAMEORIGIN, no-store; ADR-010) which differ from
    the API defaults (notably X-Frame-Options: DENY). The middleware therefore does NOT set its
    defaults on preview paths — the preview route owns its complete header set.
    """

    _PREVIEW_PREFIX = "/v1/preview/"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Response = await call_next(request)
        if request.url.path.startswith(self._PREVIEW_PREFIX):
            return response
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response
