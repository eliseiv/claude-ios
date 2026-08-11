"""Integration: POST /v1/chat/v2/run/stream SSE text streaming (ADR-069)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.integration.test_study_learn_quiz_adr064 import _pool, _quiz_result


def _parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE frames into (event, data_dict) pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if not data_lines:
            continue
        events.append((event_name, json.loads("\n".join(data_lines))))
    return events


@pytest.mark.asyncio
async def test_v2_run_stream_emits_three_deltas_then_done(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [fake_anthropic.text_result("Hello world!!")]
    fake_anthropic.stream_chunks = [["Hel", "lo ", "world!!"]]

    r = await client.post(
        "/v1/chat/v2/run/stream",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert r.headers.get("x-accel-buffering") == "no"

    events = _parse_sse(r.text)
    kinds = [e[0] for e in events]
    assert kinds[:3] == ["delta", "delta", "delta"]
    assert kinds[-1] == "done"
    assert [e[1]["text"] for e in events if e[0] == "delta"] == ["Hel", "lo ", "world!!"]

    done = events[-1][1]
    assert done["status"] == "assistant_message"
    assert done["assistantMessage"] == "Hello world!!"
    assert done["sessionId"]


@pytest.mark.asyncio
async def test_v2_run_stream_study_learn_has_no_deltas(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    pool = _pool(4, tag="S")
    fake_anthropic.responses = [
        _quiz_result(pool, text_block="Spoilers must not stream."),
        fake_anthropic.text_result("Try the cards."),
    ]
    fake_anthropic.stream_chunks = [["Spoilers ", "must ", "not stream."], ["Try ", "the cards."]]

    r = await client.post(
        "/v1/chat/v2/run/stream",
        json={
            "userId": str(uid),
            "message": "teach me",
            "mode": "credits",
            "generationMode": "study_learn",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    assert [e[0] for e in events] == ["done"], events
    done = events[0][1]
    assert done["quiz"] is not None, done
    assert done["assistantMessage"] is None
    assert len(done["quiz"]["questions"]) == 4


@pytest.mark.asyncio
async def test_v2_run_json_unchanged_with_stream_support(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """JSON /v2/run still uses create_message path (no SSE)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [fake_anthropic.text_result("plain json")]
    fake_anthropic.stream_chunks = [["should", "not", "matter"]]

    r = await client.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["assistantMessage"] == "plain json"
    # stream_chunks unused on JSON path
    assert fake_anthropic.stream_chunks == [["should", "not", "matter"]]
