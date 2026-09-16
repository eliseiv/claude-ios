"""Сквозной путь величины `chat_steps.usage` на РЕАЛЬНОЙ базе: запись и миграция `0034`.

Соседний файл (`test_chat_steps_usage_json_null.py`) закрепляет два звена по отдельности —
тарификацию неоценимого элемента и кодировщик типа колонки. Ни одно из них не доказывает, что
цепь СОЙДЁТСЯ на живой базе: кодировщик проверяется на своём же выходе, а не на том, что лежит в
строке после INSERT'а, и никакой компонентный тест не показывает, что миграция действительно
переводит накопленный JSON `null` в SQL NULL. Здесь поднимается настоящий Postgres
(testcontainers, `tests/conftest.py`), и обе величины читаются оттуда, где их читает CRM,
— условием `usage IS NULL`, тем самым, которое JSON `null` и обманывал.

Строка «как на проде» ставится СЫРЫМ SQL (`'null'::jsonb`) намеренно: через ORM её теперь не
создать — это и есть починка, — а миграции нужно дать ровно то состояние, ради которого она
написана.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import ChatStep
from tests.conftest import seed_user

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "20260916_0034_chat_steps_usage_json_null.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0034", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed_session(session: AsyncSession) -> uuid.UUID:
    user_id = await seed_user(session)
    session_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO chat_sessions (id, user_id, mode) VALUES (:s, :u, 'credits')"),
        {"s": session_id, "u": user_id},
    )
    await session.commit()
    return session_id


async def _insert_step_with_json_null(session: AsyncSession, session_id: uuid.UUID) -> uuid.UUID:
    """Шаг «как на проде до починки»: usage — JSON-скаляр `null`, а не SQL NULL."""
    step_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO chat_steps (id, session_id, message_step_id, role, payload, usage)"
            " VALUES (:id, :s, :m, 'assistant', '{}'::jsonb, 'null'::jsonb)"
        ),
        {"id": step_id, "s": session_id, "m": uuid.uuid4()},
    )
    await session.commit()
    return step_id


async def _usage_is_sql_null(session: AsyncSession, step_id: uuid.UUID) -> bool:
    value = await session.execute(
        text("SELECT usage IS NULL FROM chat_steps WHERE id = :id"), {"id": step_id}
    )
    return bool(value.scalar_one())


async def _usage_type(session: AsyncSession, step_id: uuid.UUID) -> str | None:
    value = await session.execute(
        text("SELECT jsonb_typeof(usage) FROM chat_steps WHERE id = :id"), {"id": step_id}
    )
    return value.scalar_one()


# ===================== запись: шаг без счётчиков ложится SQL NULL'ом =====================


@pytest.mark.asyncio
async def test_step_written_without_usage_lands_as_sql_null(db_session: AsyncSession) -> None:
    """Сквозной INSERT через ORM — то, чем пишет `ChatRepository.add_step`.

    Условие `usage IS NULL` — ровно то, по которому агрегат CRM решает, брать ли шаг в `usages`
    (`src/app/admin/crm_service.py`). До починки шаг проходил этот фильтр и ронял ручку.
    """
    session_id = await _seed_session(db_session)
    step = ChatStep(
        session_id=session_id,
        message_step_id=uuid.uuid4(),
        role="assistant",
        payload={"content": []},
        usage=None,
    )
    db_session.add(step)
    await db_session.commit()

    assert await _usage_is_sql_null(db_session, step.id) is True


@pytest.mark.asyncio
async def test_a_real_usage_object_is_stored_unchanged(db_session: AsyncSession) -> None:
    """Контроль: `none_as_null` касается только отсутствия, а не содержимого."""
    session_id = await _seed_session(db_session)
    step = ChatStep(
        session_id=session_id,
        message_step_id=uuid.uuid4(),
        role="assistant",
        payload={"content": []},
        usage={"model": "gpt-5.1", "inputTokens": 10},
    )
    db_session.add(step)
    await db_session.commit()

    assert await _usage_is_sql_null(db_session, step.id) is False
    assert await _usage_type(db_session, step.id) == "object"


# ===================== миграция: накопленный JSON `null` приводится =====================


@pytest.mark.asyncio
async def test_migration_0034_converts_accumulated_json_null_to_sql_null(
    db_session: AsyncSession,
) -> None:
    """Прогоняется САМА функция `upgrade()` миграции, а не её переписанный здесь SQL.

    Копия запроса в тесте доказывала бы, что копия работает. Поэтому модуль миграции грузится с
    диска и исполняется в настоящем контексте alembic на этом же соединении.
    """
    session_id = await _seed_session(db_session)
    broken = await _insert_step_with_json_null(db_session, session_id)
    assert await _usage_type(db_session, broken) == "null"  # предусловие: дефект воспроизведён

    priced = ChatStep(
        session_id=session_id,
        message_step_id=uuid.uuid4(),
        role="assistant",
        payload={"content": []},
        usage={"model": "gpt-5.1", "inputTokens": 10},
    )
    db_session.add(priced)
    await db_session.commit()

    module = _load_migration()
    connection = await db_session.connection()

    def _upgrade(sync_connection: object) -> None:
        context = MigrationContext.configure(sync_connection)  # type: ignore[arg-type]
        with Operations.context(context):
            module.upgrade()

    await connection.run_sync(_upgrade)
    await db_session.commit()

    assert await _usage_is_sql_null(db_session, broken) is True
    assert await _usage_type(db_session, priced.id) == "object"


@pytest.mark.asyncio
async def test_migration_0034_is_idempotent_on_an_already_clean_table(
    db_session: AsyncSession,
) -> None:
    """Повторный прогон не имеет что править — и не трогает оценимые шаги."""
    session_id = await _seed_session(db_session)
    priced = ChatStep(
        session_id=session_id,
        message_step_id=uuid.uuid4(),
        role="assistant",
        payload={"content": []},
        usage={"model": "gpt-5.1", "inputTokens": 10},
    )
    db_session.add(priced)
    await db_session.commit()

    module = _load_migration()
    connection = await db_session.connection()

    def _upgrade(sync_connection: object) -> None:
        context = MigrationContext.configure(sync_connection)  # type: ignore[arg-type]
        with Operations.context(context):
            module.upgrade()

    await connection.run_sync(_upgrade)
    await connection.run_sync(_upgrade)
    await db_session.commit()

    assert await _usage_type(db_session, priced.id) == "object"
