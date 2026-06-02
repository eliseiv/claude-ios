"""Gateway middleware: size limit, correlation id, security headers (api-gateway/03)."""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

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


class SizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects bodies exceeding the global limit before parsing (413)."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._limit = get_settings().size_limit_body

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._limit:
                    return self._too_large(request)
            except ValueError:
                pass
        return await call_next(request)

    def _too_large(self, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": "request body exceeds limit",
                    "requestId": request_id,
                }
            },
        )


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
