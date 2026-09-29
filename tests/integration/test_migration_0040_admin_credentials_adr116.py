"""Integration: миграция 0040 (`admin_credentials`), ADR-116 §2.2.

Своя одноразовая PostgreSQL (testcontainers): upgrade/downgrade не трогают общий контейнер
сессии. Проверяется звено цепочки (0040 ревизует 0039, голова одна), expand-only без DML,
форма таблицы и цикл upgrade → downgrade → upgrade: откат снимает ровно новую таблицу и не
трогает соседнюю операторскую, повторный подъём восстанавливает её пустой.

СИНХРОННО (без pytest-asyncio): env.py alembic сам крутит `asyncio.run`, который нельзя
вложить в работающий цикл теста (как в test_migration_0038_media_jobs_proxy_adr108).

Намеренно НЕ утверждается, что 0040 — голова: голова сдвигается с каждой новой миграцией.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_REV = "0040_admin_credentials"
_PREV = "0039_media_asset_store"
_FILE = Path("migrations/versions/20260926_0040_admin_credentials.py")


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


def _tables(url: str) -> set[str]:
    async def _go() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return set(await conn.run_sync(lambda sc: inspect(sc).get_table_names()))
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _columns(url: str) -> dict[str, dict[str, Any]]:
    async def _go() -> dict[str, dict[str, Any]]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(lambda sc: inspect(sc).get_columns("admin_credentials"))
                return {c["name"]: c for c in cols}
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def test_0040_revises_0039_and_the_chain_is_single_head() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg("postgresql+asyncpg://unused/unused"))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    rev = script.get_revision(_REV)
    assert rev.down_revision == _PREV
    assert len(_REV) <= 32, "alembic_version.version_num is VARCHAR(32)"
    assert _REV in {r.revision for r in script.walk_revisions(base="base", head=heads[0])}


def test_upgrade_body_creates_one_table_and_runs_no_dml() -> None:
    """Expand-only, без backfill: в `upgrade` только `op.create_table`."""
    tree = ast.parse(_FILE.read_text(encoding="utf-8"))
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    ops = [
        node.func.attr
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    ]
    assert ops == ["create_table"], ops


def test_upgrade_downgrade_upgrade_round_trip(isolated_pg: str) -> None:
    from alembic import command

    cfg = _cfg(isolated_pg)
    command.upgrade(cfg, _PREV)
    assert "admin_credentials" not in _tables(isolated_pg)
    # Соседняя операторская таблица с данными — откат 0040 её не трогает.
    _run(isolated_pg, "INSERT INTO admin_settings (setting_id, value) VALUES ('x.y', 'true')")

    command.upgrade(cfg, _REV)
    columns = _columns(isolated_pg)
    assert set(columns) == {
        "credential_id",
        "encrypted_value",
        "encrypted_dek",
        "fingerprint",
        "updated_at",
    }
    assert {name for name, col in columns.items() if col["nullable"]} == set()
    assert _run(isolated_pg, "SELECT count(*) FROM admin_credentials") == [(0,)]  # без засева
    _run(
        isolated_pg,
        "INSERT INTO admin_credentials (credential_id, encrypted_value, encrypted_dek, "
        "fingerprint) VALUES ('fal.api_key', '\\x00', '\\x00', 'abc')",
    )
    [(stamp,)] = _run(isolated_pg, "SELECT updated_at FROM admin_credentials")
    assert stamp is not None  # server_default now()
    with pytest.raises(Exception):  # noqa: B017 — первичный ключ credential_id
        _run(
            isolated_pg,
            "INSERT INTO admin_credentials (credential_id, encrypted_value, encrypted_dek, "
            "fingerprint) VALUES ('fal.api_key', '\\x01', '\\x01', 'def')",
        )

    command.downgrade(cfg, _PREV)
    assert "admin_credentials" not in _tables(isolated_pg)
    assert _run(isolated_pg, "SELECT count(*) FROM admin_settings") == [(1,)]

    command.upgrade(cfg, _REV)
    assert "admin_credentials" in _tables(isolated_pg)
    assert _run(isolated_pg, "SELECT count(*) FROM admin_credentials") == [(0,)]
    command.upgrade(cfg, "head")
