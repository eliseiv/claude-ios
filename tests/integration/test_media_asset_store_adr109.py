"""Integration: ADR-109 — our own 30-day copy of media results on the instance disk.

Real Postgres (testcontainers). The storage root is the test's ``tmp_path``. The source of the
bytes is faked at ``asset_store.httpx`` (store loop) and ``asset_proxy.httpx`` (download route).
Jobs reach ``completed`` through the real poll path (``GET /v1/media/jobs/{id}``) and are stored
by the real ``asset_store_tick`` — no test writes the file or sets ``stored`` itself unless the
case is explicitly about the route's reaction to a disk state.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx as _httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.media_generation import asset_store as store_mod
from app.media_generation.asset_store import (
    ADVISORY_LOCK_KEY,
    ROOT_MARKER,
    asset_path,
    asset_store_tick,
    job_dir,
)
from app.observability import metrics as m
from tests.integration.test_media_asset_proxy_adr085 import (  # noqa: F401 - fixtures
    _DOMAIN,
    _FAL_ASSET,
    _Cdn,
    _complete_video,
    _seed,
    cdn,
    fal,
    proxy_client,
)
from tests.integration.test_media_generation_adr060 import _Fal

_BYTES = b"stored-asset-bytes-0123456789"


class _Source:
    """Scripts the store loop's outgoing GET to the asset source; counts every request."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.status_code = 200
        self.body = _BYTES
        self.headers: dict[str, str] = {
            "content-type": "image/png",
            "content-length": str(len(_BYTES)),
        }
        self.exc: BaseException | None = None


def _make_source_httpx(src: _Source) -> SimpleNamespace:
    class _Resp:
        def __init__(self) -> None:
            self.status_code = src.status_code
            self.headers = _httpx.Headers(src.headers)

        async def aiter_bytes(self, _chunk: int | None = None) -> AsyncIterator[bytes]:
            yield src.body

    class _Client:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        @contextlib.asynccontextmanager
        async def stream(self, method: str, url: str) -> AsyncIterator[_Resp]:
            src.calls.append(url)
            if src.exc is not None:
                raise src.exc
            yield _Resp()

    return SimpleNamespace(AsyncClient=_Client, Timeout=_httpx.Timeout, HTTPError=_httpx.HTTPError)


async def _db_identity(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as s:
        return int(await s.scalar(text("SELECT system_identifier FROM pg_control_system()")) or 0)


def _write_marker(root: Path, identity: int | None) -> None:
    body = "" if identity is None else f"pg_system_identifier={identity}\n"
    (root / ROOT_MARKER).write_text(body, encoding="utf-8")


@pytest.fixture
def source() -> _Source:
    return _Source()


@pytest.fixture
async def storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _Source,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Path]:
    from app.db import dispose_engine

    root = tmp_path / "media-assets"
    root.mkdir()
    _write_marker(root, await _db_identity(db_sessionmaker))
    monkeypatch.setenv("MEDIA_ASSET_STORAGE_DIR", str(root))
    monkeypatch.setenv("MEDIA_ASSET_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("MEDIA_ASSET_MAX_BYTES", "1048576")
    monkeypatch.setattr(store_mod, "httpx", _make_source_httpx(source))
    get_settings.cache_clear()
    await dispose_engine()
    yield root
    await dispose_engine()


@pytest.fixture
async def sclient(storage: Path, proxy_client: AsyncClient) -> AsyncClient:  # noqa: F811
    return proxy_client


async def _row(maker: async_sessionmaker[AsyncSession], job_id: str) -> Any:
    async with maker() as s:
        return (
            await s.execute(
                text(
                    "SELECT status, asset_store_status, asset_store_attempts, "
                    "asset_store_next_attempt_at, assets_expire_at, stored_assets, "
                    "updated_at "
                    "FROM media_jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).one()


async def _exec(maker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> None:
    async with maker() as s:
        await s.execute(text(sql), params)
        await s.commit()


async def _tick() -> None:
    await asset_store_tick(get_settings())


def _val(counter: Any, **labels: str) -> float:
    return float(counter.labels(**labels)._value.get())


def _path(url: str) -> str:
    return url.removeprefix(f"https://{_DOMAIN}")


async def _stored_job(
    client: AsyncClient, fal_: _Fal, maker: async_sessionmaker[AsyncSession]
) -> tuple[str, str]:
    uid = await _seed(maker, balance=100)
    job_id, url = await _complete_video(client, fal_, uid)
    await _tick()
    row = await _row(maker, job_id)
    assert row.asset_store_status == "stored"
    return job_id, url


# ----------------------------- end-to-end chain -----------------------------


@pytest.mark.asyncio
async def test_adr109_e2e_poll_completion_tick_then_download_served_from_disk_on_source_404(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    cdn: _Cdn,  # noqa: F811
    source: _Source,
    storage: Path,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, url = await _complete_video(sclient, fal, uid)
    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "pending"
    assert row.assets_expire_at is not None

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "stored"
    assert source.calls == [_FAL_ASSET]
    assert asset_path(storage, uuid.UUID(job_id), 0).read_bytes() == _BYTES
    cdn.status_code = 404
    before = _val(m.media_asset_download_total, source="local")

    resp = await sclient.get(_path(url))

    assert resp.status_code == 200, resp.text
    assert resp.content == _BYTES
    assert cdn.calls == []
    assert _val(m.media_asset_download_total, source="local") == before + 1


@pytest.mark.asyncio
async def test_adr109_completion_expire_at_equals_completion_plus_retention(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    row = await _row(db_sessionmaker, job_id)
    delta = row.assets_expire_at - row.updated_at
    assert abs(delta - datetime.timedelta(days=30)) < datetime.timedelta(seconds=5)


@pytest.mark.asyncio
async def test_adr109_completion_with_storage_off_leaves_empty_status_and_null_expiry(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEDIA_ASSET_STORAGE_DIR", "")
    get_settings.cache_clear()
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    row = await _row(db_sessionmaker, job_id)
    assert row.status == "completed"
    assert row.asset_store_status == ""
    assert row.assets_expire_at is None


# ----------------------------- §3 outcomes -----------------------------


@pytest.mark.asyncio
async def test_adr109_host_rejected_fails_without_any_request(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    await _exec(
        db_sessionmaker,
        "UPDATE media_jobs SET result = jsonb_set(result, '{assets,0,url}', "
        "'\"https://evil.example.com/x.png\"') WHERE id = :id",
        id=job_id,
    )
    before = _val(m.media_asset_store_total, outcome="host_rejected")

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "failed"
    assert source.calls == []
    assert _val(m.media_asset_store_total, outcome="host_rejected") == before + 1


@pytest.mark.asyncio
async def test_adr109_low_disk_before_request_defers_without_request_or_attempt(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    monkeypatch.setenv("MEDIA_ASSET_MIN_FREE_BYTES", str(10**18))
    get_settings.cache_clear()

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert source.calls == []
    assert row.asset_store_status == "pending"
    assert row.asset_store_attempts == 0
    assert row.asset_store_next_attempt_at is not None


@pytest.mark.asyncio
async def test_adr109_low_disk_by_content_length_defers_keeps_pending(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    monkeypatch.setenv("MEDIA_ASSET_MIN_FREE_BYTES", "1000")
    get_settings.cache_clear()

    # The disk shrinks between the pre-check and the answer: free space is plenty until the
    # source has been asked, then the announced Content-Length no longer fits above MIN_FREE.
    def _free(_root: Path) -> int:
        return 10**15 if not source.calls else 1000 + len(_BYTES) - 1

    monkeypatch.setattr(store_mod, "_free_bytes", _free)
    before = _val(m.media_asset_store_total, outcome="deferred_low_disk")

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert len(source.calls) == 1
    assert row.asset_store_status == "pending"
    assert row.asset_store_attempts == 0
    assert row.asset_store_next_attempt_at is not None
    assert _val(m.media_asset_store_total, outcome="deferred_low_disk") == before + 1


@pytest.mark.asyncio
async def test_adr109_gone_404_fails_and_is_not_retried(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    source.status_code = 404

    await _tick()
    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "failed"
    assert row.asset_store_attempts == 0
    assert len(source.calls) == 1


@pytest.mark.asyncio
async def test_adr109_retry_on_5xx_increments_attempts_and_sets_pause(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    source.status_code = 503

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "pending"
    assert row.asset_store_attempts == 1
    assert row.asset_store_next_attempt_at > datetime.datetime.now(tz=datetime.UTC)


@pytest.mark.asyncio
async def test_adr109_exhausted_on_twelfth_attempt(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    await _exec(
        db_sessionmaker, "UPDATE media_jobs SET asset_store_attempts = 11 WHERE id = :id", id=job_id
    )
    source.exc = _httpx.ReadTimeout("t")

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "failed"
    assert row.asset_store_attempts == 12


@pytest.mark.asyncio
async def test_adr109_too_large_by_content_length_fails(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    source.headers["content-length"] = str(2 * 1048576)

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "failed"
    assert row.asset_store_attempts == 0


@pytest.mark.asyncio
async def test_adr109_too_large_without_content_length_removes_partial_file(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
    storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    monkeypatch.setenv("MEDIA_ASSET_MAX_BYTES", "4")
    get_settings.cache_clear()
    del source.headers["content-length"]

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "failed"
    d = job_dir(storage, uuid.UUID(job_id))
    assert not d.exists() or list(d.iterdir()) == []


@pytest.mark.asyncio
async def test_adr109_deferred_unavailable_when_marker_missing_rows_untouched(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
    storage: Path,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    (storage / ROOT_MARKER).unlink()
    before = _val(m.media_asset_store_total, outcome="deferred_unavailable")

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "pending"
    assert row.asset_store_attempts == 0
    assert row.asset_store_next_attempt_at is None
    assert source.calls == []
    assert _val(m.media_asset_store_total, outcome="deferred_unavailable") == before + 1


@pytest.mark.asyncio
async def test_adr109_two_ticks_one_file_identical_content(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
    storage: Path,
) -> None:
    job_id, _url = await _stored_job(sclient, fal, db_sessionmaker)
    await _tick()
    assert len(source.calls) == 1
    files = [p.name for p in job_dir(storage, uuid.UUID(job_id)).iterdir()]
    assert files == ["0"]


# ----------------------------- §5 download route -----------------------------


@pytest.mark.asyncio
async def test_adr109_download_local_range_206(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    cdn: _Cdn,  # noqa: F811
) -> None:
    _job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    resp = await sclient.get(_path(url), headers={"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.content == _BYTES[:5]
    assert resp.headers["content-range"] == f"bytes 0-4/{len(_BYTES)}"
    assert resp.headers["content-type"].startswith("image/png")
    assert cdn.calls == []


@pytest.mark.asyncio
async def test_adr109_download_local_head_has_no_body(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    cdn: _Cdn,  # noqa: F811
) -> None:
    _job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    resp = await sclient.head(_path(url))
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] == str(len(_BYTES))
    assert cdn.calls == []


@pytest.mark.asyncio
async def test_adr109_download_local_unsatisfiable_range_is_404_not_416(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
) -> None:
    _job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    resp = await sclient.get(_path(url), headers={"Range": f"bytes={len(_BYTES) + 10}-"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_adr109_download_local_if_none_match_is_full_200_not_304(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
) -> None:
    _job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    first = await sclient.get(_path(url))
    etag = first.headers["etag"]
    resp = await sclient.get(_path(url), headers={"If-None-Match": etag})
    assert resp.status_code == 200
    assert resp.content == _BYTES


@pytest.mark.asyncio
async def test_adr109_download_root_without_marker_goes_to_source_status_unchanged(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    cdn: _Cdn,  # noqa: F811
    storage: Path,
) -> None:
    job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    asset_path(storage, uuid.UUID(job_id), 0).unlink()
    (storage / ROOT_MARKER).unlink()

    resp = await sclient.get(_path(url))

    assert resp.status_code == 200
    assert resp.content == b"mp4-bytes"
    assert len(cdn.calls) == 1
    assert (await _row(db_sessionmaker, job_id)).asset_store_status == "stored"


@pytest.mark.asyncio
async def test_adr109_download_stored_without_file_turns_missing_and_goes_to_source(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    cdn: _Cdn,  # noqa: F811
    storage: Path,
) -> None:
    job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    asset_path(storage, uuid.UUID(job_id), 0).unlink()

    resp = await sclient.get(_path(url))

    assert resp.status_code == 200
    assert resp.content == b"mp4-bytes"
    assert (await _row(db_sessionmaker, job_id)).asset_store_status == "missing"


@pytest.mark.asyncio
async def test_adr109_download_missing_row_with_file_back_serves_disk_and_turns_stored(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    cdn: _Cdn,  # noqa: F811
) -> None:
    job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    await _exec(
        db_sessionmaker,
        "UPDATE media_jobs SET asset_store_status = 'missing' WHERE id = :id",
        id=job_id,
    )

    resp = await sclient.get(_path(url))

    assert resp.content == _BYTES
    assert cdn.calls == []
    assert (await _row(db_sessionmaker, job_id)).asset_store_status == "stored"


@pytest.mark.asyncio
async def test_adr109_download_storage_off_does_not_read_disk_row_stays_stored(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    cdn: _Cdn,  # noqa: F811
    storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    asset_path(storage, uuid.UUID(job_id), 0).unlink()  # would flip to missing if read
    monkeypatch.setenv("MEDIA_ASSET_STORAGE_DIR", "")
    get_settings.cache_clear()
    before = _val(m.media_asset_download_total, source="local_missing")

    resp = await sclient.get(_path(url))

    assert resp.content == b"mp4-bytes"
    assert len(cdn.calls) == 1
    assert (await _row(db_sessionmaker, job_id)).asset_store_status == "stored"
    assert _val(m.media_asset_download_total, source="local_missing") == before


@pytest.mark.asyncio
async def test_adr109_download_expired_term_goes_to_source(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    cdn: _Cdn,  # noqa: F811
) -> None:
    job_id, url = await _stored_job(sclient, fal, db_sessionmaker)
    await _exec(
        db_sessionmaker,
        "UPDATE media_jobs SET assets_expire_at = now() - interval '1 minute' WHERE id = :id",
        id=job_id,
    )
    resp = await sclient.get(_path(url))
    assert resp.content == b"mp4-bytes"
    assert len(cdn.calls) == 1


# ----------------------------- §6 cleanup -----------------------------


def _age(path: Path, seconds: float) -> None:
    ts = time.time() - seconds
    os.utime(path, (ts, ts))


def _orphan(root: Path, *, age_seconds: float) -> Path:
    jid = uuid.uuid4()
    d = job_dir(root, jid)
    d.mkdir(parents=True)
    (d / "0").write_bytes(b"x")
    _age(d, age_seconds)
    return d


@pytest.mark.asyncio
async def test_adr109_cleanup_expired_row_removes_dir_and_marks_expired(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    storage: Path,
) -> None:
    job_id, _url = await _stored_job(sclient, fal, db_sessionmaker)
    (storage / store_mod.CLEANUP_STAMP).unlink(missing_ok=True)
    await _exec(
        db_sessionmaker,
        "UPDATE media_jobs SET assets_expire_at = now() - interval '1 minute' WHERE id = :id",
        id=job_id,
    )

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "expired"
    assert row.stored_assets is None
    assert not job_dir(storage, uuid.UUID(job_id)).exists()


@pytest.mark.asyncio
async def test_adr109_cleanup_keeps_in_term_stored_and_young_orphan_removes_old_tmp(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    storage: Path,
) -> None:
    job_id, _url = await _stored_job(sclient, fal, db_sessionmaker)
    (storage / store_mod.CLEANUP_STAMP).unlink(missing_ok=True)
    young = _orphan(storage, age_seconds=60)
    tmp = job_dir(storage, uuid.UUID(job_id)) / f"{store_mod.TMP_PREFIX}abc"
    tmp.write_bytes(b"t")
    _age(tmp, 7200)

    await _tick()

    assert asset_path(storage, uuid.UUID(job_id), 0).exists()
    assert (await _row(db_sessionmaker, job_id)).asset_store_status == "stored"
    assert young.exists()
    assert not tmp.exists()


@pytest.mark.asyncio
async def test_adr109_cleanup_orphan_removed_when_identity_matches(
    storage: Path, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    old = _orphan(storage, age_seconds=7200)
    await _tick()
    assert not old.exists()
    assert m.media_asset_cleanup_blocked._value.get() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["mismatch", "absent"])
async def test_adr109_cleanup_orphans_kept_when_identity_not_confirmed(
    storage: Path,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    source: _Source,
    identity: str,
) -> None:
    real = await _db_identity(db_sessionmaker)
    _write_marker(storage, real + 1 if identity == "mismatch" else None)
    old = [_orphan(storage, age_seconds=7200) for _ in range(3)]

    await _tick()

    assert all(p.exists() for p in old)
    assert m.media_asset_cleanup_blocked._value.get() == 1

    _write_marker(storage, real)
    (storage / store_mod.CLEANUP_STAMP).unlink()
    await _tick()
    assert not any(p.exists() for p in old)
    assert m.media_asset_cleanup_blocked._value.get() == 0


@pytest.mark.asyncio
async def test_adr109_identity_query_failure_is_identity_absent(
    storage: Path,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _orphan(storage, age_seconds=7200)

    async def _fail(executor: Any) -> int | None:
        return None

    monkeypatch.setattr(store_mod, "_database_identity", _fail)
    await _tick()
    assert old.exists()
    assert m.media_asset_cleanup_blocked._value.get() == 1


# ----------------------------- §9 gauges without the execution right -----------------------


@pytest.mark.asyncio
async def test_adr109_gauges_refreshed_by_worker_without_execution_right(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,
    _engine: Any,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    await _complete_video(sclient, fal, uid)
    m.media_asset_store_pending.set(-7)
    m.media_asset_cleanup_blocked.set(-7)
    async with _engine.connect() as holder:
        got = await holder.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        await holder.commit()
        assert got
        try:
            await _tick()  # this "worker" does NOT get the right
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            await holder.commit()

    assert source.calls == []  # the executor part did not run
    assert m.media_asset_store_pending._value.get() == 1
    assert m.media_asset_cleanup_blocked._value.get() == 0


# ----------------------------- migration 0039 / settings -----------------------------


@pytest.mark.asyncio
async def test_adr109_migration_0039_defaults_and_check_rejects_foreign_value(
    sclient: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEDIA_ASSET_STORAGE_DIR", "")
    get_settings.cache_clear()
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(sclient, fal, uid)
    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT asset_store_status, asset_store_attempts, "
                    "stored_assets IS NULL AS sql_null, jsonb_typeof(stored_assets) AS jt, "
                    "assets_expire_at FROM media_jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).one()
    assert row.asset_store_status == ""
    assert row.asset_store_attempts == 0
    assert row.sql_null is True  # SQL NULL, not JSON 'null'
    assert row.assets_expire_at is None
    async with db_sessionmaker() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                text("UPDATE media_jobs SET asset_store_status = 'bogus' WHERE id = :id"),
                {"id": job_id},
            )


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "", "  "])
def test_adr109_retention_invalid_falls_back_to_30(raw: str) -> None:
    s = Settings(MEDIA_ASSET_RETENTION_DAYS=raw)  # type: ignore[call-arg]
    assert s.media_asset_retention_days() == 30


def test_adr109_retention_valid_value_is_used() -> None:
    s = Settings(MEDIA_ASSET_RETENTION_DAYS="7")  # type: ignore[call-arg]
    assert s.media_asset_retention_days() == 7
