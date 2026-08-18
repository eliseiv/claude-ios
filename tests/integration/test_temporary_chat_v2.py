"""Integration: temporary chat via POST /v1/chat/v2/run.

Temporary sessions are session-fixed at create, hidden from GET /v1/chats, still addressable by
id for multi-turn, and deleted via DELETE /v1/chats/{id}.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


async def _is_temporary(maker: async_sessionmaker[AsyncSession], session_id: str) -> bool:
    async with maker() as s:
        return bool(
            await s.scalar(
                text("SELECT is_temporary FROM chat_sessions WHERE id=:sid"),
                {"sid": session_id},
            )
        )


@pytest.mark.asyncio
async def test_temporary_v2_create_is_hidden_from_list_but_readable_by_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    fake_anthropic.responses = [
        fake_anthropic.text_result("temp ok"),
        fake_anthropic.text_result("normal ok"),
    ]

    temp = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "message": "temporary hi",
            "mode": "credits",
            "temporary": True,
        },
        headers=auth_headers(uid),
    )
    assert temp.status_code == 200, temp.text
    temp_sid = temp.json()["sessionId"]
    assert await _is_temporary(db_sessionmaker, temp_sid) is True

    normal = await client.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "normal hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert normal.status_code == 200, normal.text
    normal_sid = normal.json()["sessionId"]
    assert await _is_temporary(db_sessionmaker, normal_sid) is False

    listing = await client.get("/v1/chats", headers=auth_headers(uid))
    assert listing.status_code == 200, listing.text
    ids = {item["id"] for item in listing.json()["items"]}
    assert normal_sid in ids
    assert temp_sid not in ids

    detail = await client.get(f"/v1/chats/{temp_sid}", headers=auth_headers(uid))
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == temp_sid


@pytest.mark.asyncio
async def test_temporary_flag_ignored_on_resume(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    fake_anthropic.responses = [
        fake_anthropic.text_result("first"),
        fake_anthropic.text_result("second"),
    ]

    first = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "message": "start temporary",
            "mode": "credits",
            "temporary": True,
        },
        headers=auth_headers(uid),
    )
    assert first.status_code == 200, first.text
    sid = first.json()["sessionId"]

    second = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "sessionId": sid,
            "message": "continue",
            "mode": "credits",
            "temporary": False,
        },
        headers=auth_headers(uid),
    )
    assert second.status_code == 200, second.text
    assert second.json()["sessionId"] == sid
    assert await _is_temporary(db_sessionmaker, sid) is True


@pytest.mark.asyncio
async def test_temporary_chat_delete_cascades(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("bye")]

    created = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "message": "delete me",
            "mode": "credits",
            "temporary": True,
        },
        headers=auth_headers(uid),
    )
    assert created.status_code == 200, created.text
    sid = created.json()["sessionId"]

    deleted = await client.delete(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True

    async with db_sessionmaker() as s:
        left = await s.scalar(
            text("SELECT count(*) FROM chat_sessions WHERE id=:sid"), {"sid": sid}
        )
    assert int(left or 0) == 0

    again = await client.delete(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_legacy_run_accepts_temporary_field(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    resp = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "nope",
            "mode": "credits",
            "temporary": True,
            "dialogMode": "smart",
            "actionPrompt": "be brief",
            "history": [],
        },
        headers=auth_headers(uid),
    )
    assert resp.status_code == 200, resp.text
