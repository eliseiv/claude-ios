"""Stream a fal-hosted asset through our API without buffering the file (ADR-085).

Outgoing fetch is allowlisted (same suffixes as uploads). Redirects are not followed — a
CDN 302 to an unexpected host would otherwise be SSRF. The stored fal URL and the signed
token are never logged.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Literal

import httpx
from starlette.responses import StreamingResponse

from app.errors import GatewayTimeoutError, NotFoundError, UpstreamError
from app.media_generation.asset_hosts import fal_asset_host_allowed
from app.observability.logging import log_event

logger = logging.getLogger("app.media_generation.asset_proxy")

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 300.0
_PASS_STATUSES = frozenset({200, 206, 304})
_GONE_STATUSES = frozenset({404, 410})
_FORWARD_HEADERS = ("content-length", "content-range", "etag", "last-modified")


def _outgoing_headers(*, range_header: str | None, if_range: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if range_header:
        headers["Range"] = range_header
    if if_range:
        headers["If-Range"] = if_range
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    headers = {
        "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600",
    }
    for name in _FORWARD_HEADERS:
        value = upstream.headers.get(name)
        if value:
            headers["ETag" if name == "etag" else name.title()] = value
    return headers


async def stream_fal_asset(
    *,
    url: str,
    method: Literal["GET", "HEAD"],
    range_header: str | None,
    if_range: str | None,
    content_type_hint: str | None,
    job_id: str,
) -> StreamingResponse:
    """Open an upstream stream and wrap it. Raises AppError before the body starts."""
    if not fal_asset_host_allowed(url):
        raise NotFoundError()

    timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=30.0, pool=10.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    request = client.build_request(
        method, url, headers=_outgoing_headers(range_header=range_header, if_range=if_range)
    )
    try:
        upstream = await client.send(request, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        log_event(logger, logging.WARNING, "media_asset_upstream_timeout", jobId=job_id)
        raise GatewayTimeoutError("upstream timeout") from exc
    except httpx.HTTPError as exc:
        await client.aclose()
        log_event(logger, logging.WARNING, "media_asset_upstream_error", jobId=job_id)
        raise UpstreamError("upstream unavailable") from exc

    if upstream.status_code in _GONE_STATUSES:
        await upstream.aclose()
        await client.aclose()
        raise NotFoundError()
    if upstream.status_code not in _PASS_STATUSES:
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        if status >= 500:
            log_event(logger, logging.WARNING, "media_asset_upstream_status", jobId=job_id)
            raise UpstreamError("upstream unavailable")
        raise NotFoundError()

    media_type = (
        upstream.headers.get("content-type") or content_type_hint or "application/octet-stream"
    )

    async def chunks() -> AsyncIterator[bytes]:
        try:
            if method == "HEAD":
                return
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        media_type=media_type,
        headers=_response_headers(upstream),
    )
