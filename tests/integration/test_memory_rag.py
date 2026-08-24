"""Integration tests for cross-chat RAG memory."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.memory.embedding import get_embedding_client
from app.memory.indexer import MemoryIndexer
from app.preferences.service import PreferencesService
from tests.conftest import auth_headers, seed_user


@pytest.mark.asyncio
async def test_search_and_memories_flow(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic,
) -> None:
    async with db_sessionmaker() as session:
        uid = await seed_user(session, subscription="active", balance=20)
        prefs = PreferencesService(session)
        await prefs.patch(uid, memory_enabled=True, memory_search_scope="global")

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
        await prefs.patch(uid, memory_enabled=True, memory_search_scope="global")

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
