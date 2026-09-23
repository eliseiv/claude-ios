"""Integration: migration 0038 (media_jobs.provider / vendor_price / pending_result), ADR-108 §9.

Hermetic and ISOLATED: its own throwaway PostgreSQL container, so upgrade/downgrade cannot corrupt
the shared session container. Verifies the chain link (0038 revises 0037, the chain stays
single-head), that the migration is expand-only WITHOUT DML (the upgrade body adds three columns
and does nothing else; a row that existed before keeps every value and reads ``provider = ''``),
the column types, and that the downgrade drops exactly the three columns.

SYNC (no pytest-asyncio): alembic's env.py drives migrations under ``asyncio.run`` itself, which
cannot nest inside a running test loop (mirrors test_migration_0013_byok_provider).

Deliberately NOT asserted: that 0038 is THE head — the head advances with every new migration.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_REV = "0038_media_jobs_proxy"
_PREV = "0037_user_default_model"
_FILE = Path("migrations/versions/20260923_0038_media_jobs_proxy.py")


@pytest.fixture(scope="module")
def isolated_pg() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        yield pg.get_connection_url()


def _cfg(url: str) -> Any:
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _run(url: str, sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    async def _go() -> list[tuple[Any, ...]]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql), params or {})
                return [tuple(r) for r in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _columns(url: str) -> dict[str, dict[str, Any]]:
    async def _go() -> dict[str, dict[str, Any]]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(lambda sc: inspect(sc).get_columns("media_jobs"))
                return {c["name"]: c for c in cols}
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def test_0038_revises_0037_and_the_chain_is_single_head() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg("postgresql+asyncpg://unused/unused"))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    rev = script.get_revision(_REV)
    assert rev.revision == _REV
    assert len(_REV) <= 32, "alembic_version.version_num is VARCHAR(32)"
    assert rev.down_revision == _PREV
    # 0038 is on the path from the single head down to the base.
    assert _REV in {r.revision for r in script.walk_revisions(base="base", head=heads[0])}


def test_upgrade_body_is_three_add_columns_and_nothing_else() -> None:
    """Expand-only, no DML: no ``op.execute``/``bulk_insert``/SQL text in ``upgrade``."""
    tree = ast.parse(_FILE.read_text(encoding="utf-8"))
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    calls = [
        f"{node.func.value.id}.{node.func.attr}"  # type: ignore[union-attr]
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    ]
    assert calls == ["op.add_column", "op.add_column", "op.add_column"]
    source = ast.get_source_segment(_FILE.read_text(encoding="utf-8"), upgrade) or ""
    for dml in ("UPDATE", "INSERT", "DELETE", "execute", "bulk_insert"):
        assert dml not in source, dml


def test_upgrade_keeps_existing_rows_and_downgrade_drops_exactly_three_columns(
    isolated_pg: str,
) -> None:
    from alembic import command

    cfg = _cfg(isolated_pg)
    command.upgrade(cfg, _PREV)
    uid, job_id = uuid.uuid4(), uuid.uuid4()
    _run(isolated_pg, "INSERT INTO users (id) VALUES (:u)", {"u": uid})
    _run(
        isolated_pg,
        "INSERT INTO media_jobs (id, user_id, model_id, kind, fal_endpoint, fal_request_id, "
        "status_url, response_url, status, prompt, credits_charged) VALUES (:id, :u, "
        "'nano-banana-2', 'image', 'fal-ai/nano-banana-2', 'r1', 'https://q/s', 'https://q/r', "
        "'running', 'a cat', 4)",
        {"id": job_id, "u": uid},
    )
    before = _run(
        isolated_pg,
        "SELECT status, status_url, response_url, credits_charged, updated_at FROM media_jobs "
        "WHERE id = :id",
        {"id": job_id},
    )

    command.upgrade(cfg, _REV)

    cols = _columns(isolated_pg)
    assert str(cols["provider"]["type"]).upper() == "TEXT"
    assert cols["provider"]["nullable"] is False
    assert "''" in str(cols["provider"]["default"])
    assert str(cols["vendor_price"]["type"]).upper() == "NUMERIC(18, 6)"
    assert cols["vendor_price"]["nullable"] is True
    assert str(cols["pending_result"]["type"]).upper() == "JSONB"
    assert cols["pending_result"]["nullable"] is True
    row = _run(
        isolated_pg,
        "SELECT provider, vendor_price, pending_result FROM media_jobs WHERE id = :id",
        {"id": job_id},
    )
    assert row == [("", None, None)], "an existing row is legacy: provider '' and NULLs"
    after = _run(
        isolated_pg,
        "SELECT status, status_url, response_url, credits_charged, updated_at FROM media_jobs "
        "WHERE id = :id",
        {"id": job_id},
    )
    assert after == before, "no DML touched the existing row"

    command.downgrade(cfg, _PREV)
    remaining = set(_columns(isolated_pg))
    assert not {"provider", "vendor_price", "pending_result"} & remaining
    assert {"id", "status", "status_url", "response_url", "credits_charged"} <= remaining
    command.upgrade(cfg, "head")
