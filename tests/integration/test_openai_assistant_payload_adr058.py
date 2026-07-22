"""ADR-058: chats reads are provider-agnostic — OpenAI-shaped assistant steps render correctly.

On an OpenAI instance ``chat_steps.payload["content"]`` holds the provider's assistant MESSAGE
(``[{"role":"assistant","content":...,"tool_calls":[...]}]``) instead of domain blocks. The three
user-facing reads must behave exactly as on an Anthropic instance:

- ``GET /v1/chats`` → ``preview`` = the last message text (was ``null`` — the reported bug);
- ``GET /v1/chats/{id}`` → domain blocks (ADR-024) with domain tool ids/names (ADR-008);
- ``GET /v1/chats/{id}/steps`` → assistant summary + ``tool_call`` entries.

Steps are seeded directly via SQL (as in test_chats.py): the shape under test is written by the
OpenAI client in production, and these endpoints are read-only over the step tables.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


async def _seed_session(s: AsyncSession, *, user_id: uuid.UUID, title: str) -> uuid.UUID:
    sid = uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO chat_sessions "
            "(id, user_id, project_id, mode, title, assistant_mode, is_pinned, "
            "created_at, updated_at) "
            "VALUES (:id, :uid, 'p', 'credits', :title, 'chat', false, :ts, :ts)"
        ),
        {"id": str(sid), "uid": str(user_id), "title": title, "ts": _now()},
    )
    return sid


async def _seed_step(
    s: AsyncSession,
    *,
    session_id: uuid.UUID,
    message_step_id: uuid.UUID,
    role: str,
    payload: dict[str, Any],
    created_at: datetime.datetime | None = None,
) -> uuid.UUID:
    step_id = uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO chat_steps (id, session_id, message_step_id, role, payload, created_at) "
            "VALUES (:id, :sid, :msid, :role, CAST(:payload AS JSONB), :cre)"
        ),
        {
            "id": str(step_id),
            "sid": str(session_id),
            "msid": str(message_step_id),
            "role": role,
            "payload": json.dumps(payload),
            "cre": created_at or _now(),
        },
    )
    return step_id


async def _seed_tool_call(
    s: AsyncSession,
    *,
    session_id: uuid.UUID,
    message_step_id: uuid.UUID,
    tool_name: str,
    provider_tool_use_id: str,
) -> uuid.UUID:
    tcid = uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO tool_calls "
            "(id, session_id, message_step_id, tool_name, provider_tool_use_id, args, status) "
            "VALUES (:id, :sid, :msid, :tn, :ptu, CAST('{}' AS JSONB), 'completed')"
        ),
        {
            "id": str(tcid),
            "sid": str(session_id),
            "msid": str(message_step_id),
            "tn": tool_name,
            "ptu": provider_tool_use_id,
        },
    )
    return tcid


def _openai_assistant(content: str | None, tool_calls: list[dict[str, Any]] | None = None) -> dict:
    """The exact shape openai_client persists (LLMResult.content_blocks = [assistant message])."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"content": [message]}


@pytest.mark.asyncio
async def test_list_preview_uses_openai_assistant_text(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """GET /v1/chats: preview = the OpenAI assistant text (regression: it was null)."""
    base = _now()
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid, title="Ответь одним словом: тест")
        msid = uuid.uuid4()
        await _seed_step(
            s,
            session_id=sid,
            message_step_id=msid,
            role="user",
            payload={"content": [{"type": "text", "text": "Ответь одним словом: тест"}]},
            created_at=base,
        )
        await _seed_step(
            s,
            session_id=sid,
            message_step_id=msid,
            role="assistant",
            payload=_openai_assistant("проверка"),
            created_at=base + datetime.timedelta(seconds=1),
        )
        await s.commit()

    r = await client.get("/v1/chats", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["preview"] == "проверка"


@pytest.mark.asyncio
async def test_history_returns_domain_blocks_and_domain_tool_ids(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """GET /v1/chats/{id}: OpenAI message → text + tool_use blocks; raw call_... never leaks."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid, title="c")
        msid = uuid.uuid4()
        tcid = await _seed_tool_call(
            s,
            session_id=sid,
            message_step_id=msid,
            tool_name="site.write_file",
            provider_tool_use_id="call_abc123",
        )
        await _seed_step(
            s,
            session_id=sid,
            message_step_id=msid,
            role="assistant",
            payload=_openai_assistant(
                "пишу файл",
                [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "site_write_file",
                            "arguments": '{"path": "index.html"}',
                        },
                    }
                ],
            ),
        )
        await s.commit()

    r = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    blocks = r.json()["steps"][0]["payload"]["content"]
    assert blocks[0] == {"type": "text", "text": "пишу файл"}
    # ADR-024: dotted domain name; ADR-008: domain tool_calls.id, never the raw provider id.
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["name"] == "site.write_file"
    assert blocks[1]["id"] == str(tcid)
    assert blocks[1]["input"] == {"path": "index.html"}
    assert "call_abc123" not in r.text
    # The provider message shape itself must not survive into the content blocks.
    assert all("role" not in block for block in blocks), blocks


@pytest.mark.asyncio
async def test_steps_view_renders_openai_assistant_step(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """GET /v1/chats/{id}/steps: reasoning summary + tool_call entry with the domain name."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid, title="c")
        msid = uuid.uuid4()
        await _seed_tool_call(
            s,
            session_id=sid,
            message_step_id=msid,
            tool_name="time.now",
            provider_tool_use_id="call_t1",
        )
        await _seed_step(
            s,
            session_id=sid,
            message_step_id=msid,
            role="assistant",
            payload=_openai_assistant(
                "смотрю время",
                [
                    {
                        "id": "call_t1",
                        "type": "function",
                        "function": {"name": "time_now", "arguments": "{}"},
                    }
                ],
            ),
        )
        await s.commit()

    r = await client.get(f"/v1/chats/{sid}/steps", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    steps = r.json()["steps"]
    assert [st["kind"] for st in steps] == ["reasoning", "tool_call"]
    assert steps[0]["summary"] == "смотрю время"
    assert steps[1]["toolName"] == "time.now"
    assert "call_t1" not in r.text


@pytest.mark.asyncio
async def test_anthropic_shape_still_renders(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard: the Anthropic block shape is unaffected by the adapter."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, user_id=uid, title="c")
        msid = uuid.uuid4()
        await _seed_step(
            s,
            session_id=sid,
            message_step_id=msid,
            role="assistant",
            payload={"content": [{"type": "text", "text": "ответ Claude"}]},
        )
        await s.commit()

    lst = await client.get("/v1/chats", headers=auth_headers(uid))
    assert lst.json()["items"][0]["preview"] == "ответ Claude"
    hist = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert hist.json()["steps"][0]["payload"]["content"] == [
        {"type": "text", "text": "ответ Claude"}
    ]
