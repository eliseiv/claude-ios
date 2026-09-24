"""Our own 30-day copy of generation results on the instance disk (ADR-109).

The background loop «asset store» lives in the lifespan of EVERY gunicorn worker next to the
reconciler and runs only with a non-empty ``MEDIA_ASSET_STORAGE_DIR``. Each tick:

1. refreshes the four §9 Gauges from state shared by all processes (filesystem + ``media_jobs``)
   BEFORE trying to take the execution right — so any worker answering ``/metrics`` is right;
2. takes the execution right: a NON-blocking session-level Postgres advisory lock with a
   constant key on ONE dedicated connection (§3 inv. 1). A tick that does not get it ends here;
3. stores up to a batch of pending assets (§3) and, not more often than once per hour, cleans up
   (§6). No DB transaction stays open during a network read or a file write (§3 inv. 2): every
   statement on the held connection is committed at once, the download runs between them.

The copy is a durability layer, never a condition of the job's success (§4, §8): no outcome here
touches ``status``, credits or the ledger. Never logged: the asset URL, the token, the host path.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy import null as sa_null
from sqlalchemy import true as sa_true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import Settings, get_settings
from app.db import get_engine, get_sessionmaker
from app.media_generation.asset_hosts import fal_asset_host_allowed
from app.media_generation.repository import (
    ASSET_STORE_EXPIRED,
    ASSET_STORE_FAILED,
    ASSET_STORE_MISSING,
    ASSET_STORE_PENDING,
    ASSET_STORE_STORED,
    STATUS_COMPLETED,
)
from app.media_generation.service import MediaAsset, _assets_from_result
from app.models import MediaJob
from app.observability.logging import log_event
from app.observability.metrics import (
    media_asset_cleanup_blocked,
    media_asset_missing,
    media_asset_store_failed,
    media_asset_store_pending,
    media_asset_store_total,
    publish_media_asset_storage_gauges,
    set_media_asset_storage_free_bytes,
)

logger = logging.getLogger("app.media_generation.asset_store")

# --- ADR-109 §1.1 code constants (not settings) ---
STORE_BATCH_ASSETS = 5
STORE_MAX_ATTEMPTS = 12
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 3600
CLEANUP_INTERVAL_SECONDS = 3600
TMP_ABANDONED_SECONDS = 3600
# §6.3: an orphan directory must be older than this (race with the §3 п.6 conditional UPDATE).
ORPHAN_MIN_AGE_SECONDS = 3600
LOW_DISK_DEFER_SECONDS = 300

# §5: the marker the provisioning step puts in the storage root. The ONE predicate «storage root
# available» (download route AND outcome `deferred_unavailable`) requires it.
ROOT_MARKER = ".media-assets-root"
# §6.3: the marker line that ties the directory to ONE database cluster.
MARKER_IDENTITY_KEY = "pg_system_identifier"
_MARKER_READ_LIMIT = 4096
# Throttle of the «last cleanup» moment, shared by all workers of the instance (same directory):
# the execution right moves between workers, an in-process timestamp would not bound the rate.
CLEANUP_STAMP = ".media-assets-cleanup"
TMP_PREFIX = ".tmp-"

# Constant key of the advisory lock (§3 inv. 1). Any fixed int64 unique to this purpose.
ADVISORY_LOCK_KEY = 0x6D65_6469_6173_7431  # "mediast1"

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 300.0
_WRITE_CHUNK = 1024 * 1024
_DIR_MODE = 0o750
_FILE_MODE = 0o640
_EXPIRE_BATCH = 200
_EXPIRE_MAX_BATCHES = 25
_ORPHAN_QUERY_CHUNK = 500

EXPIRABLE_STATES = (
    ASSET_STORE_STORED,
    ASSET_STORE_PENDING,
    ASSET_STORE_FAILED,
    ASSET_STORE_MISSING,
)

Outcome = Literal[
    "deferred_unavailable",
    "host_rejected",
    "deferred_low_disk",
    "gone",
    "too_large",
    "retry",
    "exhausted",
    "stored",
]


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


# --- layout (ADR-109 §1): every segment comes from the DB row, never from a request ---


def storage_root(settings: Settings) -> Path | None:
    """The storage directory, or ``None`` when storage is off (empty setting)."""
    raw = settings.media_asset_storage_dir.strip()
    return Path(raw) if raw else None


def job_dir(root: Path, job_id: uuid.UUID) -> Path:
    """``<dir>/<first 2 chars of jobId>/<jobId>`` — built from a UUID object, no traversal."""
    name = str(job_id)
    return root / name[:2] / name


def asset_path(root: Path, job_id: uuid.UUID, index: int) -> Path:
    """``<dir>/<shard>/<jobId>/<index>`` — no extension (the content type lives in the DB)."""
    return job_dir(root, job_id) / str(int(index))


def root_available(root: Path) -> bool:
    """§5 predicate «storage root available»: exists, writable, holds the marker. Blocking I/O."""
    try:
        return root.is_dir() and os.access(root, os.W_OK) and (root / ROOT_MARKER).is_file()
    except OSError:
        return False


def retry_pause_seconds(attempts: int) -> int:
    """§1.1: ``min(2^attempts × 30 s, 3600 s)`` for the current attempt count."""
    pause: int = (2 ** max(0, attempts)) * RETRY_BASE_SECONDS
    return min(pause, RETRY_MAX_SECONDS)


def _host_allowed(url: str) -> bool:
    """§3 row 2 predicate; a URL ``urlsplit`` cannot parse is a rejected host, not an error."""
    try:
        return fal_asset_host_allowed(url)
    except ValueError:
        return False


def _free_bytes(root: Path) -> int:
    return shutil.disk_usage(root).free


# --- Gauges (§9): every worker, every tick, BEFORE the execution right ---


def read_marker_identity(root: Path) -> int | None:
    """``pg_system_identifier=<number>`` from the root marker; ``None`` = absent/unreadable.

    Blocking I/O (run via to_thread). The marker is written by provisioning (ADR-109 §Порядок
    выката п.2); only a bounded head of it is read.
    """
    try:
        with open(root / ROOT_MARKER, "rb") as handle:
            head = handle.read(_MARKER_READ_LIMIT)
    except OSError:
        return None
    for raw_line in head.decode("utf-8", errors="replace").splitlines():
        key, sep, value = raw_line.strip().partition("=")
        if sep and key.strip() == MARKER_IDENTITY_KEY:
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


async def _database_identity(executor: Any) -> int | None:
    """``system_identifier`` of the current cluster (``pg_control_system()``, EXECUTE to PUBLIC).

    ``None`` when the query itself fails (no privilege, DB error, timeout) or returns nothing —
    the identity is then NOT confirmed, which is the ``identity_absent`` outcome of §6.3: the
    failed statement is rolled back and the caller's other work goes on.
    """
    try:
        value = await executor.scalar(text("SELECT system_identifier FROM pg_control_system()"))
    except SQLAlchemyError as exc:
        await executor.rollback()
        log_event(
            logger,
            logging.WARNING,
            "media_asset_identity_query_failed",
            exceptionClass=type(exc).__name__,
        )
        return None
    return None if value is None else int(value)


def identity_verdict(
    marker: int | None, database: int | None
) -> Literal["match", "identity_absent", "identity_mismatch"]:
    """§6.3: the database is the one the directory was prepared for — or which reason not."""
    if marker is None or database is None:
        # No marker line, an unreadable marker, or the identity query failed: not confirmed.
        return "identity_absent"
    if marker != database:
        return "identity_mismatch"
    return "match"


async def refresh_gauges(root: Path) -> None:
    """Set the five §9 Gauges from the filesystem and ``media_jobs`` (shared by all workers)."""
    publish_media_asset_storage_gauges()
    try:
        free = await asyncio.to_thread(_free_bytes, root)
    except OSError:
        free = None
    # An unmeasured value is withdrawn, never exported as 0 (a false «free space low» alert).
    set_media_asset_storage_free_bytes(free)
    marker = await asyncio.to_thread(read_marker_identity, root)
    now = _now()
    in_term = MediaJob.assets_expire_at > now
    stmt = (
        select(
            func.count().filter(MediaJob.asset_store_status == ASSET_STORE_PENDING),
            func.count().filter(and_(MediaJob.asset_store_status == ASSET_STORE_FAILED, in_term)),
            func.count().filter(and_(MediaJob.asset_store_status == ASSET_STORE_MISSING, in_term)),
        )
        .select_from(MediaJob)
        .where(
            MediaJob.asset_store_status.in_(
                (ASSET_STORE_PENDING, ASSET_STORE_FAILED, ASSET_STORE_MISSING)
            )
        )
    )
    async with get_sessionmaker()() as session:
        row = (await session.execute(stmt)).one()
        await session.commit()
        database = await _database_identity(session)
        await session.commit()
    media_asset_store_pending.set(int(row[0] or 0))
    media_asset_store_failed.set(int(row[1] or 0))
    media_asset_missing.set(int(row[2] or 0))
    media_asset_cleanup_blocked.set(0 if identity_verdict(marker, database) == "match" else 1)


# --- execution right (§3 inv. 1) ---


async def _try_lock(conn: AsyncConnection) -> bool:
    """Take the execution right. Any failure after the query may leave the lock held on this
    connection, so the connection is invalidated (closed, never pooled) before re-raising."""
    try:
        got = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
        )
        await conn.commit()
    except BaseException:
        await conn.invalidate()
        raise
    return bool(got)


async def _unlock(conn: AsyncConnection) -> None:
    """Release the session-level lock before the connection returns to the pool.

    If the release itself fails the connection is invalidated (closed, not pooled): a pooled
    connection still holding the lock would block every executor of the instance forever.
    """
    try:
        await conn.rollback()
        await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})
        await conn.commit()
    except Exception:  # noqa: BLE001 - any failure → discard the connection, see docstring
        await conn.invalidate()


# --- one tick ---


@dataclasses.dataclass
class _TickState:
    """Once-per-tick bookkeeping (the `deferred_low_disk` WARNING is logged once per tick)."""

    low_disk_logged: bool = False
    assets_done: int = 0


async def asset_store_tick(settings: Settings | None = None) -> None:
    """One tick of the loop: Gauges (every worker), then store + cleanup (the executor only)."""
    settings = settings or get_settings()
    root = storage_root(settings)
    if root is None:
        return
    await refresh_gauges(root)
    conn = await get_engine().connect()
    try:
        if not await _try_lock(conn):
            return
        try:
            await _execute(conn, root, settings)
        finally:
            await _unlock(conn)
    finally:
        await conn.close()


async def _execute(conn: AsyncConnection, root: Path, settings: Settings) -> None:
    if not await asyncio.to_thread(root_available, root):
        # §3 row 1 — once per tick, rows untouched (status, attempts, next_attempt_at).
        media_asset_store_total.labels(outcome="deferred_unavailable").inc()
        log_event(
            logger,
            logging.WARNING,
            "media_asset_store_deferred",
            outcome="deferred_unavailable",
            freeBytes=None,
        )
        return
    await _store_batch(conn, root, settings)
    await _maybe_cleanup(conn, root)


# --- store (§3) ---


async def _store_batch(conn: AsyncConnection, root: Path, settings: Settings) -> None:
    now = _now()
    rows = (
        await conn.execute(
            select(
                MediaJob.id,
                MediaJob.result,
                MediaJob.assets_expire_at,
                MediaJob.asset_store_attempts,
            )
            .where(
                MediaJob.status == STATUS_COMPLETED,
                MediaJob.asset_store_status == ASSET_STORE_PENDING,
                or_(
                    MediaJob.asset_store_next_attempt_at.is_(None),
                    MediaJob.asset_store_next_attempt_at <= now,
                ),
            )
            .order_by(MediaJob.created_at.asc(), MediaJob.id.asc())
            .limit(STORE_BATCH_ASSETS)
        )
    ).all()
    await conn.commit()
    if not rows:
        return
    state = _TickState()
    timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for row in rows:
            # §1.1 batch is 5 ASSETS per tick; a job is never split (its row turns `stored` only
            # when ALL its assets are written), so the batch closes before the next job.
            if state.assets_done >= STORE_BATCH_ASSETS:
                break
            try:
                await _store_job(
                    conn,
                    client,
                    root,
                    settings,
                    state,
                    job_id=row.id,
                    result=row.result,
                    expire_at=row.assets_expire_at,
                    attempts=int(row.asset_store_attempts or 0),
                )
            except Exception as exc:  # noqa: BLE001 - one bad row must not stall the batch
                await conn.rollback()
                log_event(
                    logger,
                    logging.WARNING,
                    "media_asset_store_job_error",
                    jobId=str(row.id),
                    exceptionClass=type(exc).__name__,
                )
                # Oldest-first with a LIMIT: a row left untouched would head the queue forever.
                # Unclassified failure → the `retry` path (attempts + pause, `exhausted` at 12).
                try:
                    await _retry_or_exhaust(
                        conn,
                        row.id,
                        attempts=int(row.asset_store_attempts or 0),
                        upstream_status=None,
                    )
                except Exception:  # noqa: BLE001 - bookkeeping failure: next tick retries
                    await conn.rollback()


@dataclasses.dataclass(frozen=True)
class _Fetched:
    """Outcome of one asset download (`ok` carries what §3 п.6 records)."""

    kind: Literal["ok", "gone", "too_large", "low_disk", "retry"]
    size: int = 0
    content_type: str = ""
    upstream_status: int | None = None


async def _store_job(
    conn: AsyncConnection,
    client: httpx.AsyncClient,
    root: Path,
    settings: Settings,
    state: _TickState,
    *,
    job_id: uuid.UUID,
    result: dict[str, Any] | None,
    expire_at: datetime.datetime | None,
    attempts: int,
) -> None:
    now = _now()
    if expire_at is not None and expire_at <= now:
        # §3 inv. 3 / §6.1: past its term — not downloaded; files first, then the row.
        try:
            await asyncio.to_thread(_remove_tree, job_dir(root, job_id))
        except OSError:
            # Disk I/O error (§3 row 7): the row moves on in the queue instead of heading it.
            await _retry_or_exhaust(conn, job_id, attempts=attempts, upstream_status=None)
            return
        await _set_pending_row(
            conn, job_id, asset_store_status=ASSET_STORE_EXPIRED, stored_assets=sa_null()
        )
        return
    # The SAME list the download route indexes (`get_stored_asset`), so `<index>` agrees.
    assets = _assets_from_result(result)
    state.assets_done += len(assets)
    # §3 row 2 — without network, before ANY request of this job.
    if any(not _host_allowed(asset.url) for asset in assets):
        await _finish_failed(conn, job_id, outcome="host_rejected", attempts=attempts)
        return
    stored: list[dict[str, Any]] = []
    total = 0
    for index, asset in enumerate(assets):
        # §3 row 3 — without network: the worst case, the file size is unknown before the request.
        free = await _safe_free_bytes(root)
        if free is None:
            await _retry_or_exhaust(conn, job_id, attempts=attempts, upstream_status=None)
            return
        if free - settings.media_asset_max_bytes < settings.media_asset_min_free_bytes:
            await _defer_low_disk(conn, job_id, state, free=free)
            return
        fetched = await _fetch_asset(
            client, root, settings, job_id=job_id, index=index, asset=asset
        )
        if fetched.kind == "gone":
            await _finish_failed(
                conn,
                job_id,
                outcome="gone",
                attempts=attempts,
                upstream_status=fetched.upstream_status,
            )
            return
        if fetched.kind == "too_large":
            await _finish_failed(
                conn,
                job_id,
                outcome="too_large",
                attempts=attempts,
                upstream_status=fetched.upstream_status,
            )
            return
        if fetched.kind == "low_disk":
            await _defer_low_disk(conn, job_id, state, free=await _safe_free_bytes(root))
            return
        if fetched.kind == "retry":
            await _retry_or_exhaust(
                conn, job_id, attempts=attempts, upstream_status=fetched.upstream_status
            )
            return
        stored.append({"index": index, "bytes": fetched.size, "contentType": fetched.content_type})
        total += fetched.size
    # §3 п.6 — conditional: a row deleted meanwhile is not found; its files are §6.3 orphans.
    updated = await _set_pending_row(
        conn,
        job_id,
        asset_store_status=ASSET_STORE_STORED,
        assets_stored_at=_now(),
        assets_stored_bytes=total,
        stored_assets=stored,
    )
    if updated:
        media_asset_store_total.labels(outcome="stored").inc()
        log_event(
            logger,
            logging.INFO,
            "media_asset_stored",
            jobId=str(job_id),
            assets=len(stored),
            bytes=total,
        )


async def _safe_free_bytes(root: Path) -> int | None:
    try:
        return await asyncio.to_thread(_free_bytes, root)
    except OSError:
        return None


async def _set_pending_row(conn: AsyncConnection, job_id: uuid.UUID, **values: Any) -> bool:
    """Conditional ``UPDATE … WHERE asset_store_status = 'pending'``, own short transaction."""
    res = await conn.execute(
        update(MediaJob)
        .where(MediaJob.id == job_id, MediaJob.asset_store_status == ASSET_STORE_PENDING)
        .values(**values)
    )
    await conn.commit()
    return bool(getattr(res, "rowcount", 0))


async def _finish_failed(
    conn: AsyncConnection,
    job_id: uuid.UUID,
    *,
    outcome: Outcome,
    attempts: int,
    upstream_status: int | None = None,
) -> None:
    await _set_pending_row(conn, job_id, asset_store_status=ASSET_STORE_FAILED)
    media_asset_store_total.labels(outcome=outcome).inc()
    log_event(
        logger,
        logging.WARNING,
        "media_asset_store_failed",
        jobId=str(job_id),
        outcome=outcome,
        attempts=attempts,
        upstreamStatus=upstream_status,
    )


async def _retry_or_exhaust(
    conn: AsyncConnection, job_id: uuid.UUID, *, attempts: int, upstream_status: int | None
) -> None:
    """§3 rows 7–8: `attempts + 1 < 12` → retry with the §1.1 pause, else `exhausted`."""
    if attempts + 1 < STORE_MAX_ATTEMPTS:
        await _set_pending_row(
            conn,
            job_id,
            asset_store_attempts=attempts + 1,
            asset_store_next_attempt_at=_now()
            + datetime.timedelta(seconds=retry_pause_seconds(attempts)),
        )
        outcome: Outcome = "retry"
        new_attempts = attempts + 1
    else:
        await _set_pending_row(
            conn,
            job_id,
            asset_store_attempts=STORE_MAX_ATTEMPTS,
            asset_store_status=ASSET_STORE_FAILED,
        )
        outcome = "exhausted"
        new_attempts = STORE_MAX_ATTEMPTS
    media_asset_store_total.labels(outcome=outcome).inc()
    log_event(
        logger,
        logging.WARNING,
        "media_asset_store_failed",
        jobId=str(job_id),
        outcome=outcome,
        attempts=new_attempts,
        upstreamStatus=upstream_status,
    )


async def _defer_low_disk(
    conn: AsyncConnection, job_id: uuid.UUID, state: _TickState, *, free: int | None
) -> None:
    """§3 rows 3/6: status and attempts untouched, only ``next_attempt_at = now + 300 s``."""
    await _set_pending_row(
        conn,
        job_id,
        asset_store_next_attempt_at=_now() + datetime.timedelta(seconds=LOW_DISK_DEFER_SECONDS),
    )
    media_asset_store_total.labels(outcome="deferred_low_disk").inc()
    if not state.low_disk_logged:
        state.low_disk_logged = True
        log_event(
            logger,
            logging.WARNING,
            "media_asset_store_deferred",
            outcome="deferred_low_disk",
            freeBytes=free,
        )


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


class _TooLargeError(Exception):
    """The body read so far exceeded ``MEDIA_ASSET_MAX_BYTES`` (§3 row 5)."""


async def _fetch_asset(
    client: httpx.AsyncClient,
    root: Path,
    settings: Settings,
    *,
    job_id: uuid.UUID,
    index: int,
    asset: MediaAsset,
) -> _Fetched:
    """Download one asset into ``<index>`` atomically: tmp in the SAME dir → fsync → rename."""
    max_bytes = settings.media_asset_max_bytes
    dest_dir = job_dir(root, job_id)
    try:
        async with client.stream("GET", asset.url) as response:
            status = response.status_code
            if status in (404, 410):
                return _Fetched(kind="gone", upstream_status=status)
            if not 200 <= status < 300:
                # 3xx included: redirects are never followed (SSRF, ADR-085).
                return _Fetched(kind="retry", upstream_status=status)
            length = _content_length(response)
            if length is not None and length > max_bytes:
                return _Fetched(kind="too_large", upstream_status=status)
            if length is not None:
                free = await _safe_free_bytes(root)
                if free is None:
                    return _Fetched(kind="retry", upstream_status=status)
                if free - length < settings.media_asset_min_free_bytes:
                    return _Fetched(kind="low_disk", upstream_status=status)
            content_type = (
                response.headers.get("content-type")
                or asset.content_type
                or "application/octet-stream"
            )
            await asyncio.to_thread(_ensure_job_dir, root, job_id)
            tmp = dest_dir / f"{TMP_PREFIX}{uuid.uuid4().hex}"
            fd = await asyncio.to_thread(_open_tmp, tmp)
            size = 0
            try:
                async for chunk in response.aiter_bytes(_WRITE_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise _TooLargeError
                    await asyncio.to_thread(_write_all, fd, chunk)
                await asyncio.to_thread(_commit_tmp, fd, tmp, dest_dir / str(index))
            except BaseException:
                await asyncio.to_thread(_discard_tmp, fd, tmp)
                raise
            return _Fetched(kind="ok", size=size, content_type=content_type, upstream_status=status)
    except _TooLargeError:
        return _Fetched(kind="too_large")
    except httpx.HTTPError:
        return _Fetched(kind="retry")
    except OSError:
        # A disk I/O error is a `retry` fact (§3 row 7), never a failure of the asset.
        return _Fetched(kind="retry")


# --- blocking file helpers (run via asyncio.to_thread) ---


def _mkdir(path: Path) -> None:
    try:
        path.mkdir(mode=_DIR_MODE)
    except FileExistsError:
        return
    os.chmod(path, _DIR_MODE)  # mkdir honours the umask; §10 wants exactly 0750


def _ensure_job_dir(root: Path, job_id: uuid.UUID) -> None:
    target = job_dir(root, job_id)
    _mkdir(target.parent)
    _mkdir(target)


def _open_tmp(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, _FILE_MODE)
    if hasattr(os, "fchmod"):  # open() honours the umask; §10 wants exactly 0640
        os.fchmod(fd, _FILE_MODE)
    return fd


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _commit_tmp(fd: int, tmp: Path, final: Path) -> None:
    os.fsync(fd)
    os.close(fd)
    os.replace(tmp, final)
    _fsync_dir(final.parent)


def _fsync_dir(path: Path) -> None:
    """Make the rename durable. Directories cannot be opened on every platform — best effort."""
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _discard_tmp(fd: int, tmp: Path) -> None:
    with contextlib.suppress(OSError):
        os.close(fd)
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()


def _tree_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _remove_tree(path: Path) -> int:
    """Remove ``path`` recursively; ENOENT is not an error (§6.1). Returns the bytes freed."""
    freed = _tree_size(path)
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return 0
    return freed


# --- cleanup (§6) ---


def _cleanup_due(root: Path) -> bool:
    """At most once per hour per INSTANCE: the stamp lives in the shared storage directory."""
    stamp = root / CLEANUP_STAMP
    try:
        age = time.time() - stamp.stat().st_mtime
    except FileNotFoundError:
        age = float("inf")
    if age < CLEANUP_INTERVAL_SECONDS:
        return False
    stamp.touch()
    return True


@dataclasses.dataclass
class _Scan:
    job_dirs: list[tuple[uuid.UUID, Path, float]]
    tmp_removed: int
    tmp_freed: int


def _scan_tree(root: Path, now_ts: float) -> _Scan:
    """Walk ``<root>/<shard>/<jobId>``: collect job directories, drop tmp files older than 1 h.

    Only names this module could have produced are considered (a 2-hex shard holding a canonical
    UUID whose first two chars ARE the shard); the marker, the stamp and anything else stay.
    """
    job_dirs: list[tuple[uuid.UUID, Path, float]] = []
    tmp_removed = 0
    tmp_freed = 0
    try:
        shards = list(os.scandir(root))
    except OSError:
        return _Scan(job_dirs, 0, 0)
    for shard in shards:
        if len(shard.name) != 2 or not shard.is_dir(follow_symlinks=False):
            continue
        try:
            entries = list(os.scandir(shard.path))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or entry.name[:2] != shard.name:
                continue
            try:
                job_id = uuid.UUID(entry.name)
            except ValueError:
                continue
            if str(job_id) != entry.name:
                continue
            try:
                inner = list(os.scandir(entry.path))
            except OSError:
                continue
            for item in inner:
                if not item.name.startswith(TMP_PREFIX):
                    continue
                try:
                    st = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                if now_ts - st.st_mtime <= TMP_ABANDONED_SECONDS:
                    continue
                try:
                    os.unlink(item.path)
                except OSError:
                    continue
                tmp_removed += 1
                tmp_freed += st.st_size
            try:
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            job_dirs.append((job_id, Path(entry.path), mtime))
    return _Scan(job_dirs, tmp_removed, tmp_freed)


async def _maybe_cleanup(conn: AsyncConnection, root: Path) -> None:
    try:
        due = await asyncio.to_thread(_cleanup_due, root)
    except OSError:
        return
    if not due:
        return
    expired, freed = await _cleanup_expired(conn, root)
    scan = await asyncio.to_thread(_scan_tree, root, time.time())
    freed += scan.tmp_freed
    orphans_removed, orphans_freed = await _cleanup_orphans(conn, root, scan)
    freed += orphans_freed
    log_event(
        logger,
        logging.INFO,
        "media_asset_cleanup",
        expired=expired,
        orphans=orphans_removed,
        tmp=scan.tmp_removed,
        freedBytes=freed,
    )


async def _cleanup_expired(conn: AsyncConnection, root: Path) -> tuple[int, int]:
    """§6.1: files first, then ``expired`` + ``stored_assets = NULL`` (idempotent on replay)."""
    expired = 0
    freed = 0
    skipped: set[uuid.UUID] = set()
    for _ in range(_EXPIRE_MAX_BATCHES):
        now = _now()
        ids = list(
            (
                await conn.scalars(
                    select(MediaJob.id)
                    .where(
                        MediaJob.asset_store_status.in_(EXPIRABLE_STATES),
                        MediaJob.assets_expire_at < now,
                        MediaJob.id.notin_(skipped) if skipped else sa_true(),
                    )
                    .order_by(MediaJob.assets_expire_at.asc())
                    .limit(_EXPIRE_BATCH)
                )
            ).all()
        )
        await conn.commit()
        if not ids:
            break
        removed: list[uuid.UUID] = []
        for job_id in ids:
            try:
                freed += await asyncio.to_thread(_remove_tree, job_dir(root, job_id))
            except OSError as exc:
                # Files first, then the row: a directory we could not remove keeps its row, is
                # skipped for the rest of this run, and the other rows proceed.
                skipped.add(job_id)
                _log_cleanup_error("expired", job_id, exc)
                continue
            removed.append(job_id)
        if not removed:
            if len(ids) < _EXPIRE_BATCH:
                break
            continue
        res = await conn.execute(
            update(MediaJob)
            .where(
                MediaJob.id.in_(removed),
                MediaJob.asset_store_status.in_(EXPIRABLE_STATES),
                MediaJob.assets_expire_at < now,
            )
            .values(asset_store_status=ASSET_STORE_EXPIRED, stored_assets=sa_null())
        )
        await conn.commit()
        expired += int(getattr(res, "rowcount", 0) or 0)
        if len(ids) < _EXPIRE_BATCH:
            break
    return expired, freed


async def _cleanup_orphans(conn: AsyncConnection, root: Path, scan: _Scan) -> tuple[int, int]:
    """§6.3: job directories without a ``media_jobs`` row, older than an hour.

    Runs ONLY if the marker's ``pg_system_identifier`` equals the current database's
    ``system_identifier`` — «no row» from an empty or foreign database would otherwise wipe the
    whole copy. Match → the step runs in full, without thresholds (a legitimate mass deletion is
    not blocked). No match / no line / unreadable marker → ONLY this step is skipped, WARNING
    ``media_asset_cleanup_aborted`` (the Gauge ``media_asset_cleanup_blocked`` is set every tick
    by ``refresh_gauges``).
    """
    marker = await asyncio.to_thread(read_marker_identity, root)
    database = await _database_identity(conn)
    await conn.commit()
    verdict = identity_verdict(marker, database)
    if verdict != "match":
        media_asset_cleanup_blocked.set(1)
        log_event(logger, logging.WARNING, "media_asset_cleanup_aborted", reason=verdict)
        return 0, 0
    if not scan.job_dirs:
        return 0, 0
    existing: set[uuid.UUID] = set()
    ids = [job_id for job_id, _path, _mtime in scan.job_dirs]
    for start in range(0, len(ids), _ORPHAN_QUERY_CHUNK):
        chunk = ids[start : start + _ORPHAN_QUERY_CHUNK]
        found = await conn.scalars(select(MediaJob.id).where(MediaJob.id.in_(chunk)))
        existing.update(found.all())
        await conn.commit()
    now_ts = time.time()
    orphans = [
        (job_id, path)
        for job_id, path, mtime in scan.job_dirs
        if job_id not in existing and now_ts - mtime > ORPHAN_MIN_AGE_SECONDS
    ]
    freed = 0
    removed = 0
    for job_id, path in orphans:
        try:
            freed += await asyncio.to_thread(_remove_tree, path)
        except OSError as exc:
            _log_cleanup_error("orphan", job_id, exc)
            continue
        removed += 1
    return removed, freed


def _log_cleanup_error(step: str, job_id: uuid.UUID, exc: OSError) -> None:
    """One cleanup item failed (e.g. EACCES); the step goes on with the others (§6(а))."""
    log_event(
        logger,
        logging.WARNING,
        "media_asset_cleanup_error",
        step=step,
        jobId=str(job_id),
        errno=exc.errno,
    )


# --- the loop ---


async def asset_store_loop(stop: asyncio.Event, settings: Settings | None = None) -> None:
    """Run ``asset_store_tick`` every ``MEDIA_ASSET_STORE_INTERVAL_SECONDS`` until ``stop``.

    Not started at all (``main.lifespan``) with an empty storage dir or an interval ``<= 0``.
    """
    settings = settings or get_settings()
    interval = settings.media_asset_store_interval_seconds
    if interval <= 0 or storage_root(settings) is None:
        return
    log_event(logger, logging.INFO, "media_asset_store_started", intervalSeconds=interval)
    while not stop.is_set():
        try:
            await asset_store_tick(settings)
        except Exception as exc:  # noqa: BLE001 - the loop must survive any tick failure
            log_event(
                logger,
                logging.WARNING,
                "media_asset_store_loop_error",
                exceptionClass=type(exc).__name__,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
    log_event(logger, logging.INFO, "media_asset_store_stopped")


__all__ = [
    "ADVISORY_LOCK_KEY",
    "ROOT_MARKER",
    "asset_path",
    "asset_store_loop",
    "asset_store_tick",
    "job_dir",
    "refresh_gauges",
    "root_available",
    "storage_root",
]
