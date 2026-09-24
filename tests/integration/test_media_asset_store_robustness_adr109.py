"""Integration: ADR-109 robustness of the asset-store loop (review fixes).

Reuses the harness of ``test_media_asset_store_adr109``: real Postgres, ``tmp_path`` storage
root, the source faked at ``asset_store.httpx``, jobs completed through the real poll path.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.media_generation import asset_store as store_mod
from app.media_generation.asset_store import CLEANUP_STAMP, asset_path, job_dir
from tests.integration.test_media_asset_proxy_adr085 import (  # noqa: F401 - fixtures
    _complete_video,
    _seed,
    cdn,
    fal,
    proxy_client,
)
from tests.integration.test_media_asset_store_adr109 import (  # noqa: F401 - fixtures
    _exec,
    _orphan,
    _row,
    _Source,
    _stored_job,
    _tick,
    sclient,
    source,
    storage,
)
from tests.integration.test_media_generation_adr060 import _Fal


def _warned(caplog: pytest.LogCaptureFixture, event: str) -> bool:
    return any(event in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_adr109_unclassified_job_error_goes_retry_and_next_row_processed(
    sclient: AsyncClient,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    bad_id, _ = await _complete_video(sclient, fal, uid)
    good_id, _ = await _complete_video(sclient, fal, uid)
    real = store_mod._assets_from_result
    calls = {"n": 0}

    def _flaky(result: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:  # oldest-first: the first row of the batch
            raise RuntimeError("boom")
        return real(result)

    monkeypatch.setattr(store_mod, "_assets_from_result", _flaky)

    await _tick()

    bad = await _row(db_sessionmaker, bad_id)
    assert bad.asset_store_status == "pending"
    assert bad.asset_store_attempts == 1
    assert bad.asset_store_next_attempt_at > datetime.datetime.now(tz=datetime.UTC)
    assert (await _row(db_sessionmaker, good_id)).asset_store_status == "stored"


@pytest.mark.asyncio
async def test_adr109_unparsable_url_is_host_rejected_without_request(
    sclient: AsyncClient,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,  # noqa: F811
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _ = await _complete_video(sclient, fal, uid)
    await _exec(
        db_sessionmaker,
        "UPDATE media_jobs SET result = jsonb_set(result, '{assets,0,url}', "
        "'\"https://[bad\"') WHERE id = :id",
        id=job_id,
    )

    await _tick()

    assert (await _row(db_sessionmaker, job_id)).asset_store_status == "failed"
    assert source.calls == []


@pytest.mark.asyncio
async def test_adr109_expired_pending_with_permission_error_goes_retry(
    sclient: AsyncClient,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    source: _Source,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _ = await _complete_video(sclient, fal, uid)
    await _exec(
        db_sessionmaker,
        "UPDATE media_jobs SET assets_expire_at = now() - interval '1 minute' WHERE id = :id",
        id=job_id,
    )

    def _deny(_path: Path) -> int:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(store_mod, "_remove_tree", _deny)

    await _tick()

    row = await _row(db_sessionmaker, job_id)
    assert row.asset_store_status == "pending"
    assert row.asset_store_attempts == 1
    assert row.asset_store_next_attempt_at > datetime.datetime.now(tz=datetime.UTC)
    assert source.calls == []


@pytest.mark.asyncio
async def test_adr109_cleanup_expired_one_dir_fails_others_expire(
    sclient: AsyncClient,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    storage: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad_id, _ = await _stored_job(sclient, fal, db_sessionmaker)
    good_id, _ = await _stored_job(sclient, fal, db_sessionmaker)
    (storage / CLEANUP_STAMP).unlink(missing_ok=True)
    for jid in (bad_id, good_id):
        await _exec(
            db_sessionmaker,
            "UPDATE media_jobs SET assets_expire_at = now() - interval '1 minute' WHERE id = :id",
            id=jid,
        )
    real = store_mod._remove_tree
    bad_dir = job_dir(storage, uuid.UUID(bad_id))

    def _remove(path: Path) -> int:
        if path == bad_dir:
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(store_mod, "_remove_tree", _remove)
    caplog.set_level(logging.WARNING)

    await _tick()

    assert (await _row(db_sessionmaker, good_id)).asset_store_status == "expired"
    assert not job_dir(storage, uuid.UUID(good_id)).exists()
    assert (await _row(db_sessionmaker, bad_id)).asset_store_status == "stored"
    assert asset_path(storage, uuid.UUID(bad_id), 0).exists()
    assert _warned(caplog, "media_asset_cleanup_error")


@pytest.mark.asyncio
async def test_adr109_cleanup_orphans_one_fails_others_removed(
    storage: Path,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = _orphan(storage, age_seconds=7200)
    good = _orphan(storage, age_seconds=7200)
    real = store_mod._remove_tree

    def _remove(path: Path) -> int:
        if path == bad:
            raise OSError(5, "io")
        return real(path)

    monkeypatch.setattr(store_mod, "_remove_tree", _remove)
    caplog.set_level(logging.WARNING)

    await _tick()

    assert bad.exists()
    assert not good.exists()
    assert _warned(caplog, "media_asset_cleanup_error")
