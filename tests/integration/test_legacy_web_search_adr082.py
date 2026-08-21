"""Integration: hosted web search on legacy POST /v1/chat/run when the instance opts in."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user


@pytest.mark.asyncio
async def test_legacy_run_default_stays_general_without_web_search(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    settings = get_settings()
    original = settings.chat_legacy_web_search_enabled
    settings.chat_legacy_web_search_enabled = False
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active", balance=10)
        fake_anthropic.responses = [fake_anthropic.text_result("plain answer")]
        r = await client.post(
            "/v1/chat/run",
            json={
                "userId": str(uid),
                "projectId": "p",
                "message": "what is the weather in Paris",
                "mode": "credits",
            },
            headers=auth_headers(uid),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "assistant_message"
        assert fake_anthropic.calls[-1]["generation_mode"] == "general"
        assert all(t.get("name") != "web_search" for t in fake_anthropic.calls[-1]["tools"])
        from app.chat.orchestrator import _RESEARCH_INSTRUCTION

        assert _RESEARCH_INSTRUCTION not in fake_anthropic.calls[-1]["system_prompt"]
        async with db_sessionmaker() as s:
            bal = await s.scalar(
                text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)}
            )
        assert int(bal) == 9
    finally:
        settings.chat_legacy_web_search_enabled = original


@pytest.mark.asyncio
async def test_legacy_run_with_flag_attaches_web_search_and_charges_research(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    settings = get_settings()
    original = (
        settings.chat_legacy_web_search_enabled,
        settings.chat_credit_cost_research,
    )
    settings.chat_legacy_web_search_enabled = True
    settings.chat_credit_cost_research = 3
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active", balance=10)
        fake_anthropic.responses = [fake_anthropic.text_result("search answer")]
        r = await client.post(
            "/v1/chat/run",
            json={
                "userId": str(uid),
                "projectId": "p",
                "message": "what is the weather in Paris",
                "mode": "credits",
            },
            headers=auth_headers(uid),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "assistant_message"
        assert fake_anthropic.calls[-1]["generation_mode"] == "research"
        assert any(t.get("name") == "web_search" for t in fake_anthropic.calls[-1]["tools"])
        from app.chat.orchestrator import _RESEARCH_INSTRUCTION

        assert _RESEARCH_INSTRUCTION in fake_anthropic.calls[-1]["system_prompt"]
        # Legacy response does not expose usage.generationMode / creditsCharged.
        usage = body.get("usage") or {}
        assert "generationMode" not in usage
        assert "creditsCharged" not in usage
        async with db_sessionmaker() as s:
            bal = await s.scalar(
                text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)}
            )
        assert int(bal) == 7
    finally:
        (
            settings.chat_legacy_web_search_enabled,
            settings.chat_credit_cost_research,
        ) = original


@pytest.mark.asyncio
async def test_legacy_flag_does_not_rewrite_v2_general(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    settings = get_settings()
    original = settings.chat_legacy_web_search_enabled
    settings.chat_legacy_web_search_enabled = True
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active", balance=10)
        fake_anthropic.responses = [fake_anthropic.text_result("v2 general")]
        r = await client.post(
            "/v1/chat/v2/run",
            json={
                "userId": str(uid),
                "projectId": "p",
                "message": "hello",
                "mode": "credits",
                "generationMode": "general",
            },
            headers=auth_headers(uid),
        )
        assert r.status_code == 200
        assert r.json()["usage"]["generationMode"] == "general"
        assert r.json()["usage"]["creditsCharged"] == 1
        assert fake_anthropic.calls[-1]["generation_mode"] == "general"
        assert all(t.get("name") != "web_search" for t in fake_anthropic.calls[-1]["tools"])
        from app.chat.orchestrator import _RESEARCH_INSTRUCTION

        assert _RESEARCH_INSTRUCTION not in fake_anthropic.calls[-1]["system_prompt"]
    finally:
        settings.chat_legacy_web_search_enabled = original
