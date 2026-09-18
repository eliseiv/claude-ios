"""Integration tests for cross-chat RAG memory."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import dispose_engine
from app.memory.embedding import get_embedding_client
from app.memory.indexer import MemoryIndexer
from app.memory.repository import MemoryRepository
from app.models import ChatChunk, ChatSession
from app.preferences.service import PreferencesService
from tests.conftest import auth_headers, seed_user


@pytest.fixture(autouse=True)
async def _enable_memory_for_tests(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Память включена ТОЛЬКО на время этих тестов — и не оставляет за собой ничего живого.

    Оба следа приходится убирать руками, иначе полный прогон ВИСНЕТ на следующем тесте:

    * **Флаг.** `memory_enabled` живёт в `lru_cache` `get_settings`: `monkeypatch` вернёт
      переменную окружения, но закэшированные `Settings` не пересоберёт.
    * **Фоновые задачи.** Ход чата планирует индексацию через `asyncio.create_task`
      (`schedule_index_turn`) на СОБСТВЕННОМ `get_sessionmaker()`, а не на сессии теста. Не
      доработав до закрытия петли, задача оставляет соединение брошенным с открытой
      транзакцией — и `TRUNCATE` фикстуры следующего теста встаёт в блокировку НАВСЕГДА
      (`chat_steps` держит даже `ACCESS SHARE` от `SELECT`), то есть полный прогон висит без
      единого падения. Поэтому задачи дожидаются, а глобальный движок утилизируется:
      следующий тест получает чистый пул на своей петле.
    """
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    get_settings.cache_clear()
    get_embedding_client.cache_clear()

    yield

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=10)
    await dispose_engine()
    monkeypatch.undo()
    get_settings.cache_clear()
    get_embedding_client.cache_clear()


@pytest.mark.asyncio
async def test_search_and_memories_flow(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic,
) -> None:
    async with db_sessionmaker() as session:
        uid = await seed_user(session, subscription="active", balance=20)
        prefs = PreferencesService(session)
        # ADR-091: персональной настройки памяти больше нет — гейт только инстансный, и его
        # включает фикстура _enable_memory_for_tests выше. На пользователе включать нечего.
        await prefs.patch(uid, memory_search_scope="global")

    fake_anthropic.responses = [fake_anthropic.text_result("indexed reply")]
    headers = auth_headers(uid)

    run1 = await client.post(
        "/v1/chat/run",
        headers=headers,
        json={
            "userId": str(uid),
            "message": "SwiftUI navigation stack notes",
            "mode": "credits",
            "assistantMode": "chat",
        },
    )
    assert run1.status_code == 200
    session_id = run1.json()["sessionId"]

    async with db_sessionmaker() as session:
        indexer = MemoryIndexer(session, get_embedding_client(), get_settings())
        await indexer.index_turn(uuid.UUID(session_id), uuid.UUID(run1.json()["messageStepId"]))

    search = await client.get(
        "/v1/search",
        headers=headers,
        params={"q": "SwiftUI navigation"},
    )
    assert search.status_code == 200
    results = search.json()["results"]
    assert len(results) >= 1
    assert results[0]["sessionId"] == session_id

    create = await client.post(
        "/v1/memories",
        headers=headers,
        json={"content": "User prefers SwiftUI"},
    )
    assert create.status_code == 201
    memory_id = create.json()["memory"]["id"]

    listed = await client.get("/v1/memories", headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == memory_id for item in listed.json()["items"])

    deleted = await client.delete(f"/v1/memories/{memory_id}", headers=headers)
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_memory_search_in_system_prompt(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic,
) -> None:
    async with db_sessionmaker() as session:
        uid = await seed_user(session, subscription="active", balance=20)
        prefs = PreferencesService(session)
        # ADR-091: персональной настройки памяти больше нет — гейт только инстансный, и его
        # включает фикстура _enable_memory_for_tests выше. На пользователе включать нечего.
        await prefs.patch(uid, memory_search_scope="global")

    headers = auth_headers(uid)
    fake_anthropic.responses = [fake_anthropic.text_result("seed")]
    seed = await client.post(
        "/v1/chat/run",
        headers=headers,
        json={
            "userId": str(uid),
            "message": "PostgreSQL indexing strategy",
            "mode": "credits",
        },
    )
    assert seed.status_code == 200
    sid = seed.json()["sessionId"]
    msid = seed.json()["messageStepId"]

    async with db_sessionmaker() as session:
        indexer = MemoryIndexer(session, get_embedding_client(), get_settings())
        await indexer.index_turn(uuid.UUID(sid), uuid.UUID(msid))

    fake_anthropic.responses = [fake_anthropic.text_result("with memory")]
    out = await client.post(
        "/v1/chat/v2/run",
        headers=headers,
        json={
            "userId": str(uid),
            "message": "Что мы обсуждали про базу данных?",
            "mode": "credits",
            "memorySearch": True,
        },
    )
    assert out.status_code == 200
    assert fake_anthropic.calls
    system = fake_anthropic.calls[-1]["system_prompt"]
    assert "past conversations" in system.lower() or "PostgreSQL" in system


# --------------------------------------------------------------------------------------------
# Персистентность кусков шага: MemoryRepository.upsert_chunks
#
# Ниже — репозиторный уровень, а не ручка: оба инварианта живут ИМЕННО в `upsert_chunks`, и
# через API их не наблюсти. Один шаг индексируется двумя независимыми путями — фоновой задачей
# `schedule_index_turn` (собственный sessionmaker) и явным `index_turn`/`backfill_user`, — то
# есть в БД одновременно пишут ДВА соединения.
# --------------------------------------------------------------------------------------------


def _vec(seed: float) -> list[float]:
    """Вектор нужной размерности (`chat_chunks.embedding` — `Vector(1536)`)."""
    return [seed] + [0.0] * 1535


async def _seed_session(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Пользователь и чат: у `chat_chunks` FK на `users.id` и `chat_sessions.id`."""
    async with db_sessionmaker() as session:
        uid = await seed_user(session)
        chat = ChatSession(user_id=uid, mode="credits")
        session.add(chat)
        await session.commit()
        return uid, chat.id


async def _chunks_of_step(
    db_sessionmaker: async_sessionmaker[AsyncSession], chat_step_id: uuid.UUID
) -> list[tuple[int, str]]:
    async with db_sessionmaker() as session:
        rows = await session.execute(
            select(ChatChunk.chunk_index, ChatChunk.text)
            .where(ChatChunk.chat_step_id == chat_step_id)
            .order_by(ChatChunk.chunk_index)
        )
        return [(int(idx), str(txt)) for idx, txt in rows.all()]


async def _write_chunks(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    chat_step_id: uuid.UUID,
    message_step_id: uuid.UUID,
    chunks: list[tuple[int, str, list[float]]],
) -> None:
    async with db_sessionmaker() as session:
        await MemoryRepository(session).upsert_chunks(
            user_id=user_id,
            session_id=session_id,
            chat_step_id=chat_step_id,
            message_step_id=message_step_id,
            workspace_project_id=None,
            session_title="t",
            role="user",
            chunks=chunks,
        )
        await session.commit()


@pytest.mark.asyncio
async def test_upsert_chunks_reindex_to_fewer_chunks_drops_tail(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Переиндексация шага в МЕНЬШЕЕ число кусков сносит хвостовые номера.

    Инвариант: после записи N кусков в шаге не остаётся кусков с `chunk_index >= N`. Иначе
    хвост прошлой (более длинной) индексации остаётся живым и попадает в векторный поиск как
    актуальный фрагмент — молча, потому что поиск идёт по `chat_chunks`, а не по шагу. Второй
    проверяемый здесь же инвариант — куски с сохранившимися номерами ПЕРЕЗАПИСЫВАЮТСЯ новым
    текстом, а не остаются от прошлой индексации.
    """
    uid, session_id = await _seed_session(db_sessionmaker)
    step_id, message_step_id = uuid.uuid4(), uuid.uuid4()

    await _write_chunks(
        db_sessionmaker,
        user_id=uid,
        session_id=session_id,
        chat_step_id=step_id,
        message_step_id=message_step_id,
        chunks=[(0, "old-0", _vec(0.1)), (1, "old-1", _vec(0.2)), (2, "old-2", _vec(0.3))],
    )
    assert await _chunks_of_step(db_sessionmaker, step_id) == [
        (0, "old-0"),
        (1, "old-1"),
        (2, "old-2"),
    ]

    await _write_chunks(
        db_sessionmaker,
        user_id=uid,
        session_id=session_id,
        chat_step_id=step_id,
        message_step_id=message_step_id,
        chunks=[(0, "new-0", _vec(0.4)), (1, "new-1", _vec(0.5))],
    )

    assert await _chunks_of_step(db_sessionmaker, step_id) == [(0, "new-0"), (1, "new-1")]


@pytest.mark.asyncio
async def test_upsert_chunks_reindex_to_zero_chunks_drops_all(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Пустой список кусков снимает шаг с индекса целиком.

    Отдельный кейс, потому что запись идёт по отдельной ветке (`if chunks:` пропускает вставку),
    и без него «ноль кусков» молча перестал бы что-либо чистить.
    """
    uid, session_id = await _seed_session(db_sessionmaker)
    step_id, message_step_id = uuid.uuid4(), uuid.uuid4()

    await _write_chunks(
        db_sessionmaker,
        user_id=uid,
        session_id=session_id,
        chat_step_id=step_id,
        message_step_id=message_step_id,
        chunks=[(0, "a", _vec(0.1)), (1, "b", _vec(0.2))],
    )
    await _write_chunks(
        db_sessionmaker,
        user_id=uid,
        session_id=session_id,
        chat_step_id=step_id,
        message_step_id=message_step_id,
        chunks=[],
    )

    assert await _chunks_of_step(db_sessionmaker, step_id) == []


@pytest.mark.asyncio
async def test_upsert_chunks_concurrent_indexing_of_same_step_keeps_one_row_per_index(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Две одновременные индексации ОДНОГО шага не роняют транзакцию на уникальном ограничении.

    Гонка ставится ДЕТЕРМИНИРОВАННО, а не расчётом на удачу планировщика. Соединение A пишет
    куски и НЕ фиксирует транзакцию; соединение B начинает писать те же `(chat_step_id,
    chunk_index)` и упирается в блокировку на `uq_chat_chunks_step_chunk`: незафиксированную
    строку A оно не видит, но и вставить поверх её ключа не может. Дожидаемся именно этого
    состояния (опрос `pg_stat_activity`, а не `sleep` на глазок), затем фиксируем A — с этого
    момента исход определён устройством запроса B, а не таймингом.

    «Удалить всё и вставить» здесь ОБЯЗАН упасть с `UniqueViolationError`: DELETE у B не увидел
    незафиксированных строк A, значит удалять было нечего, а INSERT приходит на ключ, к тому
    моменту уже зафиксированный. Идемпотентная запись обязана пройти и оставить ровно одну
    строку на номер.
    """
    uid, session_id = await _seed_session(db_sessionmaker)
    step_id, message_step_id = uuid.uuid4(), uuid.uuid4()

    async def _upsert(session: AsyncSession, tag: str) -> None:
        await MemoryRepository(session).upsert_chunks(
            user_id=uid,
            session_id=session_id,
            chat_step_id=step_id,
            message_step_id=message_step_id,
            workspace_project_id=None,
            session_title="t",
            role="user",
            chunks=[(0, tag + "-0", _vec(0.1)), (1, tag + "-1", _vec(0.2))],
        )

    async def _wait_until_blocked(watcher: AsyncSession, pid: int) -> bool:
        """True, как только СОЕДИНЕНИЕ B встало в ожидание блокировки (иначе — тайм-аут).

        Ждём КОНКРЕТНЫЙ бэкенд по его `pg_backend_pid()`, а не «любой заблокированный в этой
        базе»: в полном прогоне в той же базе живут соединения других тестов, и общий счётчик
        дал бы ложное срабатывание — A зафиксировали бы раньше, чем B упёрся в ключ, и гонка
        осталась бы непоставленной под зелёным результатом.
        """
        for _ in range(400):  # до ~20 c; штатно срабатывает на первых итерациях
            blocked = await watcher.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE pid = :pid AND wait_event_type = 'Lock'"
                ),
                {"pid": pid},
            )
            if blocked and int(blocked) > 0:
                return True
            await asyncio.sleep(0.05)
        return False

    async with (
        db_sessionmaker() as session_a,
        db_sessionmaker() as session_b,
        db_sessionmaker() as watcher,
    ):
        pid_b = await session_b.scalar(text("SELECT pg_backend_pid()"))
        assert pid_b is not None

        await _upsert(session_a, "a")  # записано, НЕ зафиксировано

        task_b = asyncio.create_task(_upsert(session_b, "b"))
        try:
            assert await _wait_until_blocked(watcher, int(pid_b)), (
                "конкурирующая запись не встала в ожидание блокировки — гонка не поставлена, "
                "и зелёный этого кейса ничего не доказывает"
            )
            await session_a.commit()
            await task_b  # прежняя реализация падает здесь: UniqueViolationError
            await session_b.commit()
        finally:
            if not task_b.done():
                task_b.cancel()

    async with db_sessionmaker() as session:
        total = await session.scalar(
            select(func.count()).select_from(ChatChunk).where(ChatChunk.chat_step_id == step_id)
        )
    assert total == 2
    assert [idx for idx, _ in await _chunks_of_step(db_sessionmaker, step_id)] == [0, 1]
