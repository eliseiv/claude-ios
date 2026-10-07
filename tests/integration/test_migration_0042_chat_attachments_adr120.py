"""Integration: миграция 0042 (`chat_attachments`), ADR-120 §1.

Своя одноразовая PostgreSQL (testcontainers): общий контейнер сессии не трогается. Звено цепочки
(0042 ревизует 0041, голова одна), форма таблицы, уникальный индекс хода, CASCADE по сессии и
цикл upgrade → downgrade → upgrade. Голову намеренно не пиним: она сдвигается.

СИНХРОННО: env.py alembic сам крутит `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_REV = "0042_chat_attachments"
_PREV = "0041_media_jobs_remaining_routes"


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


def _inspect(url: str, fn: Any) -> Any:
    async def _go() -> Any:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(lambda sc: fn(inspect(sc)))
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def test_0042_revises_0041_and_the_chain_is_single_head() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_cfg("postgresql+asyncpg://unused/unused"))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert script.get_revision(_REV).down_revision == _PREV
    assert len(_REV) <= 32
    assert _REV in {r.revision for r in script.walk_revisions(base="base", head=heads[0])}


def _seed_session(url: str) -> uuid.UUID:
    uid, sid = uuid.uuid4(), uuid.uuid4()
    _run(url, "INSERT INTO users (id) VALUES (:id)", {"id": uid})
    _run(
        url,
        "INSERT INTO chat_sessions (id, user_id, mode) VALUES (:id, :u, 'credits')",
        {"id": sid, "u": uid},
    )
    return sid


def test_upgrade_downgrade_upgrade_round_trip(isolated_pg: str) -> None:
    from alembic import command

    cfg = _cfg(isolated_pg)
    command.upgrade(cfg, _PREV)
    assert "chat_attachments" not in _inspect(isolated_pg, lambda i: i.get_table_names())

    command.upgrade(cfg, _REV)
    cols = {
        c["name"]: c for c in _inspect(isolated_pg, lambda i: i.get_columns("chat_attachments"))
    }
    assert set(cols) == {
        "id",
        "user_id",
        "session_id",
        "message_step_id",
        "position",
        "type",
        "media_type",
        "filename",
        "size_bytes",
        "content",
        "created_at",
    }
    assert not [n for n, c in cols.items() if c["nullable"]]
    indexes = {
        ix["name"]: ix for ix in _inspect(isolated_pg, lambda i: i.get_indexes("chat_attachments"))
    }
    ux = indexes["ux_chat_attachments_turn"]
    assert ux["unique"] and ux["column_names"] == ["session_id", "message_step_id", "position"]

    sid = _seed_session(isolated_pg)
    [(uid,)] = _run(isolated_pg, "SELECT user_id FROM chat_sessions WHERE id = :s", {"s": sid})
    step = uuid.uuid4()
    insert = (
        "INSERT INTO chat_attachments (user_id, session_id, message_step_id, position, type, "
        "media_type, filename, size_bytes, content) "
        "VALUES (:u, :s, :m, 0, 'text', 'text/plain', 'a.txt', 1, '\x41')"
    )
    _run(isolated_pg, insert, {"u": uid, "s": sid, "m": step})
    with pytest.raises(Exception):  # noqa: B017 — уникальный (session, step, position)
        _run(isolated_pg, insert, {"u": uid, "s": sid, "m": step})
    _run(isolated_pg, "DELETE FROM chat_sessions WHERE id = :s", {"s": sid})
    assert _run(isolated_pg, "SELECT count(*) FROM chat_attachments") == [(0,)]  # CASCADE

    command.downgrade(cfg, _PREV)
    assert "chat_attachments" not in _inspect(isolated_pg, lambda i: i.get_table_names())
    command.upgrade(cfg, "head")
    assert "chat_attachments" in _inspect(isolated_pg, lambda i: i.get_table_names())
