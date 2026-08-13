"""Integration: opt-in dual-credits OpenAI+Anthropic (ADR-073).

Unset ``LLM_PROVIDERS`` keeps the single-provider catalog and 422 for a foreign model — live
instances are unchanged. With ``LLM_PROVIDERS`` + both keys, GET /v1/models unions both
allowlists (additive ``provider``) and /chat/run routes by the session-fixed model. Resume
still ignores the request ``model`` (no mid-chat provider switch).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.chat.llm_client as llm_mod
from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.integration.test_byok_multiprovider_generation_adr044 import FakeOpenAIClient


@pytest.fixture
def restore_dual_settings() -> Iterator[None]:
    s = get_settings()
    orig = (
        s.llm_provider,
        s.llm_providers_raw,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.openai_model,
        s.openai_api_key,
        s.anthropic_api_key,
    )
    yield
    (
        s.llm_provider,
        s.llm_providers_raw,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.openai_model,
        s.openai_api_key,
        s.anthropic_api_key,
    ) = orig


def _enable_openai_instance(*, dual: bool) -> None:
    s = get_settings()
    s.llm_provider = "openai"
    s.openai_api_key = "sk-openai-service-test"
    s.anthropic_api_key = "sk-ant-service-test"
    s.openai_model = "gpt-4o"
    s.anthropic_model = "claude-sonnet-4-5"
    s.openai_models_raw = json.dumps({"gpt-4o": "GPT-4o", "gpt-4o-mini": "GPT-4o mini"})
    s.anthropic_models_raw = json.dumps({"claude-sonnet-4-5": "Claude Sonnet 4.5"})
    s.llm_providers_raw = "openai,anthropic" if dual else ""


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAIClient:
    fake = FakeOpenAIClient()
    monkeypatch.setattr(llm_mod, "_openai_singleton", fake)
    monkeypatch.setattr(llm_mod, "_openai_responses_singleton", fake)
    return fake


async def _session_model(maker: async_sessionmaker[AsyncSession], session_id: str) -> str | None:
    async with maker() as s:
        return await s.scalar(
            text("SELECT model FROM chat_sessions WHERE id=:sid"), {"sid": session_id}
        )


@pytest.mark.asyncio
async def test_catalog_without_llm_providers_stays_single_provider(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_dual_settings: None,
) -> None:
    _enable_openai_instance(dual=False)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    assert [m["id"] for m in models] == ["gpt-4o", "gpt-4o-mini"]
    assert models[0]["default"] is True
    assert all(m["provider"] == "openai" for m in models)


@pytest.mark.asyncio
async def test_catalog_with_llm_providers_unions_both_allowlists(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_dual_settings: None,
) -> None:
    _enable_openai_instance(dual=True)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    ids = [m["id"] for m in models]
    assert ids[0] == "gpt-4o"
    assert models[0]["default"] is True
    assert models[0]["provider"] == "openai"
    assert "gpt-4o-mini" in ids
    assert "claude-sonnet-4-5" in ids
    by_id = {m["id"]: m for m in models}
    assert by_id["claude-sonnet-4-5"]["provider"] == "anthropic"
    assert by_id["claude-sonnet-4-5"]["default"] is False
    assert sum(1 for m in models if m["default"]) == 1


@pytest.mark.asyncio
async def test_create_claude_without_llm_providers_is_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_openai: FakeOpenAIClient,
    restore_dual_settings: None,
) -> None:
    _enable_openai_instance(dual=False)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "hi",
            "mode": "credits",
            "model": "claude-sonnet-4-5",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert fake_anthropic.calls == []
    assert fake_openai.calls == []


@pytest.mark.asyncio
async def test_dual_credits_routes_gpt_to_openai_and_claude_to_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_openai: FakeOpenAIClient,
    restore_dual_settings: None,
) -> None:
    _enable_openai_instance(dual=True)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    fake_openai.responses = [fake_openai.text_result("gpt ok")]
    fake_anthropic.responses = [fake_anthropic.text_result("claude ok")]

    r_gpt = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi gpt", "mode": "credits", "model": "gpt-4o"},
        headers=auth_headers(uid),
    )
    assert r_gpt.status_code == 200, r_gpt.text
    assert fake_openai.calls[-1]["model"] == "gpt-4o"
    assert len(fake_anthropic.calls) == 0

    r_claude = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "hi claude",
            "mode": "credits",
            "model": "claude-sonnet-4-5",
        },
        headers=auth_headers(uid),
    )
    assert r_claude.status_code == 200, r_claude.text
    assert fake_anthropic.calls[-1]["model"] == "claude-sonnet-4-5"
    assert len(fake_openai.calls) == 1  # Claude did not call OpenAI


@pytest.mark.asyncio
async def test_dual_credits_resume_ignores_model_no_provider_switch(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_openai: FakeOpenAIClient,
    restore_dual_settings: None,
) -> None:
    _enable_openai_instance(dual=True)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    fake_anthropic.responses = [
        fake_anthropic.text_result("first"),
        fake_anthropic.text_result("second"),
    ]

    r1 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "one",
            "mode": "credits",
            "model": "claude-sonnet-4-5",
        },
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200, r1.text
    sess = r1.json()["sessionId"]
    assert await _session_model(db_sessionmaker, sess) == "claude-sonnet-4-5"

    r2 = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "sessionId": sess,
            "message": "two",
            "mode": "credits",
            "model": "gpt-4o",
        },
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text
    assert await _session_model(db_sessionmaker, sess) == "claude-sonnet-4-5"
    assert fake_anthropic.calls[-1]["model"] == "claude-sonnet-4-5"
    assert fake_openai.calls == []
