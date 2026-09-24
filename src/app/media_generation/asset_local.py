"""Download route source selection: our own copy on disk, else the source (ADR-109 §5).

Step order after the row lookup and the token check (both in the router, unchanged):

3. storage off (empty ``MEDIA_ASSET_STORAGE_DIR``) → the disk is not read at all → source path;
4. own copy: ``asset_store_status ∈ {stored, missing} ∧ now < assets_expire_at`` →
   * storage root unavailable (no directory / not writable / no ``.media-assets-root`` marker) →
     WARNING ``media_asset_storage_unavailable`` (≤ once a minute per worker), status NOT changed,
     source path (``source=remote``);
   * the file reads → bytes from disk (``source=local``); a ``missing`` row flips back to
     ``stored`` with a conditional UPDATE (the transition is reversible);
   * root available, row ``stored``, the OS says «no such file» → WARNING
     ``media_asset_local_missing``, conditional UPDATE to ``missing`` (``source=local_missing``),
     source path; a row already ``missing`` goes to the source silently (``source=remote``);
   * any other read error → WARNING, status NOT changed, source path (``source=remote``);
5. otherwise → ``stream_fal_asset`` exactly as before ADR-109 (``source=remote``).

The local response is built here, not by Starlette's ``FileResponse``: that class answers
``416`` for an unsatisfiable ``Range`` and ``304`` for ``If-None-Match``, both codes the route has
never returned. Norm (§5): unsatisfiable ``Range`` → ``404 not_found``; ``If-None-Match`` /
``If-Modified-Since`` are ignored (never ``304``); ``If-Range`` with a non-matching validator →
full ``200``. The header set equals the source path's.

Status transitions run in their OWN short session, committed at once: the request session lives
until the response has been streamed, and a row lock held that long would stall the asset-store
loop's conditional updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import email.utils
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.errors import NotFoundError
from app.media_generation.asset_proxy import stream_fal_asset
from app.media_generation.asset_store import asset_path, root_available, storage_root
from app.media_generation.repository import ASSET_STORE_MISSING, ASSET_STORE_STORED
from app.media_generation.service import MediaAsset
from app.models import MediaJob
from app.observability.logging import log_event
from app.observability.metrics import media_asset_download_total

logger = logging.getLogger("app.media_generation.asset_local")

_READ_CHUNK = 256 * 1024
_UNAVAILABLE_LOG_INTERVAL_SECONDS = 60.0
_LOCAL_STATES = frozenset({ASSET_STORE_STORED, ASSET_STORE_MISSING})
_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)

# Per-worker throttle of `media_asset_storage_unavailable` (ADR-109 §9: ≤ once a minute).
_last_unavailable_log = 0.0


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


@dataclasses.dataclass
class _Opened:
    fd: int
    size: int
    mtime: float
    closed: bool = False


@dataclasses.dataclass(frozen=True)
class _Probe:
    kind: Literal["ok", "root_unavailable", "not_found", "read_error"]
    opened: _Opened | None = None
    errno: int | None = None


def _probe(root: Path, job_id: uuid.UUID, index: int) -> _Probe:
    """Blocking: the §5 root predicate, then open + fstat the file (run via to_thread)."""
    if not root_available(root):
        return _Probe(kind="root_unavailable")
    path = asset_path(root, job_id, index)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return _Probe(kind="not_found")
    except OSError as exc:
        return _Probe(kind="read_error", errno=exc.errno)
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        return _Probe(kind="read_error", errno=exc.errno)
    if not os.path.isfile(path):
        os.close(fd)
        return _Probe(kind="read_error")
    return _Probe(kind="ok", opened=_Opened(fd=fd, size=st.st_size, mtime=st.st_mtime))


def _content_type(job: MediaJob, index: int, asset: MediaAsset) -> str:
    """``stored_assets[index].contentType`` (§3 п.6 already applied the fallback order)."""
    for item in job.stored_assets or []:
        if isinstance(item, dict) and item.get("index") == index:
            value = item.get("contentType")
            if isinstance(value, str) and value:
                return value
    return asset.content_type or "application/octet-stream"


def _etag(opened: _Opened) -> str:
    return f'"{opened.size:x}-{int(opened.mtime * 1_000_000):x}"'


def _last_modified(opened: _Opened) -> str:
    return email.utils.formatdate(opened.mtime, usegmt=True)


def _parse_range(header: str, size: int) -> tuple[int, int] | None | Literal["unsatisfiable"]:
    """One ``bytes=`` range → ``(start, end)`` inclusive; ``None`` = serve the whole file.

    A header this parser does not understand (multi-range, other unit, garbage) is ignored — the
    full body is a valid answer to any ``Range`` (RFC 9110 §14.2). A well-formed but
    unsatisfiable range is ``"unsatisfiable"`` → the caller answers ``404``.
    """
    match = _RANGE_RE.match(header)
    if match is None:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:
        suffix = int(last)
        if suffix == 0 or size == 0:
            return "unsatisfiable"
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if last and end < start:
        return None
    if start >= size:
        return "unsatisfiable"
    return start, min(end, size - 1)


def _read_at(fd: int, offset: int, length: int) -> bytes:
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


def _local_response(
    opened: _Opened,
    *,
    method: Literal["GET", "HEAD"],
    range_header: str | None,
    if_range: str | None,
    content_type: str,
) -> StreamingResponse:
    """Build the disk response; raises ``NotFoundError`` for an unsatisfiable range (fd closed)."""
    etag = _etag(opened)
    last_modified = _last_modified(opened)
    byte_range: tuple[int, int] | None = None
    if range_header and (if_range is None or if_range.strip() in (etag, last_modified)):
        parsed = _parse_range(range_header, opened.size)
        if parsed == "unsatisfiable":
            _close_once(opened)
            raise NotFoundError()
        byte_range = parsed
    headers = {
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600",
        "ETag": etag,
        "Last-Modified": last_modified,
    }
    if byte_range is None:
        start, end, status = 0, opened.size - 1, 200
    else:
        start, end = byte_range
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{opened.size}"
    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)

    async def chunks() -> AsyncIterator[bytes]:
        try:
            if method == "HEAD":
                return
            offset = start
            remaining = length
            while remaining > 0:
                data = await asyncio.to_thread(
                    _read_at, opened.fd, offset, min(_READ_CHUNK, remaining)
                )
                if not data:
                    return
                offset += len(data)
                remaining -= len(data)
                yield data
        finally:
            await asyncio.to_thread(_close_once, opened)

    return _FileStreamingResponse(
        opened, chunks(), status_code=status, media_type=content_type, headers=headers
    )


def _close_once(opened: _Opened) -> None:
    """Close the descriptor exactly once (generator ``finally`` and response teardown race)."""
    if opened.closed:
        return
    opened.closed = True
    with contextlib.suppress(OSError):
        os.close(opened.fd)


class _FileStreamingResponse(StreamingResponse):
    """Owns the open file: closes it however the response ends — even when the body iterator
    never started (client gone before the first chunk, send failed), which the generator's
    ``finally`` alone does not cover."""

    def __init__(self, opened: _Opened, content: AsyncIterator[bytes], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._opened = opened

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.to_thread(_close_once, self._opened)


async def _transition(job_id: uuid.UUID, *, from_status: str, to_status: str) -> None:
    """Conditional ``UPDATE … SET asset_store_status = :to WHERE … = :from`` (idempotent)."""
    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                update(MediaJob)
                .where(MediaJob.id == job_id, MediaJob.asset_store_status == from_status)
                .values(asset_store_status=to_status)
            )
            await session.commit()
    except SQLAlchemyError as exc:
        # A failed bookkeeping write must not fail the download; the next one retries it.
        log_event(
            logger,
            logging.WARNING,
            "media_asset_status_update_failed",
            jobId=str(job_id),
            exceptionClass=type(exc).__name__,
        )


def _log_storage_unavailable() -> None:
    global _last_unavailable_log
    now = time.monotonic()
    if now - _last_unavailable_log < _UNAVAILABLE_LOG_INTERVAL_SECONDS:
        return
    _last_unavailable_log = now
    log_event(logger, logging.WARNING, "media_asset_storage_unavailable")


async def serve_media_asset(
    *,
    job: MediaJob,
    index: int,
    asset: MediaAsset,
    method: Literal["GET", "HEAD"],
    range_header: str | None,
    if_range: str | None,
    settings: Settings | None = None,
) -> StreamingResponse:
    """Steps 3–5 of ADR-109 §5 for an already found row with a valid token."""
    settings = settings or get_settings()
    root = storage_root(settings)
    status = job.asset_store_status
    expire_at = job.assets_expire_at
    if (
        root is not None
        and status in _LOCAL_STATES
        and expire_at is not None
        and _now() < expire_at
    ):
        probe = await asyncio.to_thread(_probe, root, job.id, index)
        if probe.kind == "ok" and probe.opened is not None:
            opened = probe.opened
            try:
                response = _local_response(
                    opened,
                    method=method,
                    range_header=range_header,
                    if_range=if_range,
                    content_type=_content_type(job, index, asset),
                )
                if status == ASSET_STORE_MISSING:
                    await _transition(
                        job.id, from_status=ASSET_STORE_MISSING, to_status=ASSET_STORE_STORED
                    )
            except BaseException:
                _close_once(opened)
                raise
            media_asset_download_total.labels(source="local").inc()
            return response
        if probe.kind == "root_unavailable":
            _log_storage_unavailable()
        elif probe.kind == "not_found" and status == ASSET_STORE_STORED:
            log_event(
                logger,
                logging.WARNING,
                "media_asset_local_missing",
                jobId=str(job.id),
                index=index,
            )
            await _transition(job.id, from_status=ASSET_STORE_STORED, to_status=ASSET_STORE_MISSING)
            media_asset_download_total.labels(source="local_missing").inc()
            return await _remote(job, asset, method, range_header, if_range, count=False)
        elif probe.kind == "read_error":
            log_event(
                logger,
                logging.WARNING,
                "media_asset_local_read_error",
                jobId=str(job.id),
                index=index,
                errno=probe.errno,
            )
    return await _remote(job, asset, method, range_header, if_range, count=True)


async def _remote(
    job: MediaJob,
    asset: MediaAsset,
    method: Literal["GET", "HEAD"],
    range_header: str | None,
    if_range: str | None,
    *,
    count: bool,
) -> StreamingResponse:
    if count:
        media_asset_download_total.labels(source="remote").inc()
    return await stream_fal_asset(
        url=asset.url,
        method=method,
        range_header=range_header,
        if_range=if_range,
        content_type_hint=asset.content_type,
        job_id=str(job.id),
    )
