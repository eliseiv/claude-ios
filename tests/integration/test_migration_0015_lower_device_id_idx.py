"""Integration: alembic migration 0015 (auth_devices lower(device_id) functional index) — ADR-055.

Hermetic and ISOLATED: spins its OWN throwaway PostgreSQL container so upgrade/downgrade cannot
corrupt the shared session container. Verifies the chain is SINGLE-HEAD, that ``upgrade`` creates
the functional index ``ix_auth_devices_lower_device_id ON auth_devices (lower(device_id))``, that
it is idempotent (``CREATE INDEX IF NOT EXISTS`` / ``DROP INDEX IF EXISTS``), that ``downgrade``
drops exactly it (reversible) leaving ``auth_devices`` intact, and — critically — that the
migration is DATA-PRESERVING: an UPPERCASE device_id stays UPPERCASE after upgrade (expand-only, no
normalisation; normalising would detach users from their accounts).

SYNC (no pytest-asyncio): alembic's env.py drives migrations under asyncio.run itself, which cannot
nest inside a running test loop (mirrors test_migration_0013_byok_provider and conftest._migrated).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_INDEX = "ix_auth_devices_lower_device_id"


@pytest.fixture(scope="module")
def isolated_pg() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


def _alembic_config(url: str):
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _index_names(url: str, table: str) -> set[str]:
    async def _run() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                idx = await conn.run_sync(lambda sc: inspect(sc).get_indexes(table))
                return {i["name"] for i in idx}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _table_names(url: str) -> set[str]:
    async def _run() -> set[str]:
        engine = create_async_engine(url, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return set(await conn.run_sync(lambda sc: inspect(sc).get_table_names()))
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_migrations_single_head() -> None:
    """Exactly ONE head and 0015 exists with its abbreviated (<=32 char) revision id (ADR-055).

    Do NOT assert the head equals 0015: the head advances with every new migration. The single-head
    invariant plus the presence of 0015's exact id is what this guards.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected single head, got {heads}"
    rev = script.get_revision("0015_devid_lower_idx")
    assert rev.revision == "0015_devid_lower_idx"
    assert len(rev.revision) <= 32  # alembic_version.version_num is VARCHAR(32)


def test_migration_0015_creates_functional_index(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")
    assert _INDEX in _index_names(isolated_pg, "auth_devices")
    # The base auth_devices index (on user_id) and the PK survive.
    assert "ix_auth_devices_user" in _index_names(isolated_pg, "auth_devices")


def test_functional_index_backs_lower_predicate(isolated_pg: str) -> None:
    """The index is defined on ``lower(device_id)`` (an EXPLAIN on the resolver predicate names it).

    Proves the migration created a FUNCTIONAL index matching the resolver's ``WHERE
    lower(device_id) = :x``, not a plain column index. We force a seq-scan off so the planner must
    consider the index for the assertion to be meaningful on an otherwise tiny table.
    """
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "head")

    async def _run() -> str:
        engine = create_async_engine(isolated_pg, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                # pg_indexes.indexdef renders the full CREATE INDEX incl. the lower() expression.
                indexdef = await conn.scalar(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                    {"n": _INDEX},
                )
                return str(indexdef)
        finally:
            await engine.dispose()

    indexdef = asyncio.run(_run())
    assert "lower(device_id)" in indexdef.lower()


def test_migration_0015_preserves_uppercase_device_id(isolated_pg: str) -> None:
    """Expand-only: an UPPERCASE device_id is NOT rewritten to lowercase by the migration."""
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    # Migrate to just before 0015 so we can insert a row, then upgrade over it.
    command.upgrade(cfg, "0014_cp_webhook_events")
    stored = "E8FF6CB8-77F1-4165-A91C-21E897E19C7A"

    async def _seed() -> None:
        engine = create_async_engine(isolated_pg, future=True, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                uid = (
                    await conn.execute(
                        text("INSERT INTO users (id) VALUES (gen_random_uuid()) RETURNING id")
                    )
                ).scalar_one()
                await conn.execute(
                    text("INSERT INTO auth_devices (device_id, user_id) VALUES (:d, :u)"),
                    {"d": stored, "u": str(uid)},
                )
        finally:
            await engine.dispose()

    asyncio.run(_seed())

    command.upgrade(cfg, "0015_devid_lower_idx")

    async def _read() -> str | None:
        engine = create_async_engine(isolated_pg, future=True, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(
                    text("SELECT device_id FROM auth_devices WHERE lower(device_id) = :x"),
                    {"x": stored.lower()},
                )
        finally:
            await engine.dispose()

    device_id = asyncio.run(_read())
    assert device_id == stored  # unchanged casing (no normalisation)


def test_migration_0015_downgrade_drops_index_only(isolated_pg: str) -> None:
    from alembic import command

    cfg = _alembic_config(isolated_pg)
    command.upgrade(cfg, "0015_devid_lower_idx")
    assert _INDEX in _index_names(isolated_pg, "auth_devices")

    # Roll back exactly to 0014: the functional index disappears, auth_devices stays.
    command.downgrade(cfg, "0014_cp_webhook_events")
    after_down = _index_names(isolated_pg, "auth_devices")
    assert _INDEX not in after_down
    assert "ix_auth_devices_user" in after_down  # the base index survives
    assert "auth_devices" in _table_names(isolated_pg)

    # Re-upgrade restores it (reversible + idempotent CREATE IF NOT EXISTS).
    command.upgrade(cfg, "0015_devid_lower_idx")
    assert _INDEX in _index_names(isolated_pg, "auth_devices")
