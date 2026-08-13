"""Integration: credits key failover (ADR-074).

Empty backup / empty crossover model → previous single-key behaviour (live instances).
A banned OpenAI primary rotates to the spare OpenAI key; both OpenAI keys dead +
``OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL`` set → Anthropic answers the GPT request.
``LLM_PROVIDER`` / catalog are unchanged. BYOK is not rotated.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.chat.llm_client as llm_mod
from app.config import get_settings
from app.errors import UpstreamError
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.integration.test_byok_multiprovider_generation_adr044 import FakeOpenAIClient

OPENAI_PRIMARY = "sk-openai-primary-test"
OPENAI_BACKUP = "sk-openai-backup-test"
ANTHROPIC_PRIMARY = "sk-ant-service-test"
ANTHROPIC_BACKUP = "sk-ant-backup-test"


class _Status(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(body)
        self.status_code = status_code
        self.body = body


@pytest.fixture
def restore_failover_settings() -> Iterator[None]:
    s = get_settings()
    orig = (
        s.llm_provider,
        s.llm_providers_raw,
        s.openai_api_key,
        s.openai_api_key_backup,
        s.anthropic_api_key,
        s.anthropic_api_key_backup,
        s.openai_chat_fallback_anthropic_model,
        s.anthropic_chat_fallback_openai_model,
        s.openai_model,
        s.anthropic_model,
        s.openai_models_raw,
        s.anthropic_models_raw,
    )
    yield
    (
        s.llm_provider,
        s.llm_providers_raw,
        s.openai_api_key,
        s.openai_api_key_backup,
        s.anthropic_api_key,
        s.anthropic_api_key_backup,
        s.openai_chat_fallback_anthropic_model,
        s.anthropic_chat_fallback_openai_model,
        s.openai_model,
        s.anthropic_model,
        s.openai_models_raw,
        s.anthropic_models_raw,
    ) = orig


def _enable_openai_failover(*, crossover: bool, backup: bool = True) -> None:
    s = get_settings()
    s.llm_provider = "openai"
    s.llm_providers_raw = ""
    s.openai_api_key = OPENAI_PRIMARY
    s.openai_api_key_backup = OPENAI_BACKUP if backup else ""
    s.anthropic_api_key = ANTHROPIC_PRIMARY
    s.anthropic_api_key_backup = ANTHROPIC_BACKUP
    s.openai_model = "gpt-4o"
    s.anthropic_model = "claude-sonnet-4-5"
    s.openai_models_raw = json.dumps({"gpt-4o": "GPT-4o"})
    s.anthropic_models_raw = json.dumps({"claude-sonnet-4-5": "Claude Sonnet 4.5"})
    s.openai_chat_fallback_anthropic_model = "claude-sonnet-4-5" if crossover else ""
    s.anthropic_chat_fallback_openai_model = "gpt-4o" if crossover else ""


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAIClient:
    fake = FakeOpenAIClient()
    monkeypatch.setattr(llm_mod, "_openai_singleton", fake)
    monkeypatch.setattr(llm_mod, "_openai_responses_singleton", fake)
    return fake


@pytest.mark.asyncio
async def test_dead_openai_primary_rotates_to_backup_without_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_openai: FakeOpenAIClient,
    restore_failover_settings: None,
) -> None:
    _enable_openai_failover(crossover=True)
    fake_openai.auth_error_keys = {OPENAI_PRIMARY}
    fake_openai.responses = [fake_openai.text_result("from backup")]
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "model": "gpt-4o"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["assistantMessage"] == "from backup"
    assert [c["api_key"] for c in fake_openai.calls] == [OPENAI_PRIMARY, OPENAI_BACKUP]
    assert fake_anthropic.calls == []


@pytest.mark.asyncio
async def test_both_openai_keys_dead_moves_the_request_to_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_openai: FakeOpenAIClient,
    restore_failover_settings: None,
) -> None:
    _enable_openai_failover(crossover=True)
    fake_openai.auth_error_keys = {OPENAI_PRIMARY, OPENAI_BACKUP}
    fake_anthropic.responses = [fake_anthropic.text_result("claude answered")]
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "model": "gpt-4o"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["assistantMessage"] == "claude answered"
    assert [c["api_key"] for c in fake_openai.calls] == [OPENAI_PRIMARY, OPENAI_BACKUP]
    assert fake_anthropic.calls[-1]["api_key"] == ANTHROPIC_PRIMARY
    assert fake_anthropic.calls[-1]["model"] == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_without_crossover_model_a_dead_openai_pair_does_not_call_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_openai: FakeOpenAIClient,
    restore_failover_settings: None,
) -> None:
    _enable_openai_failover(crossover=False)
    fake_openai.auth_error_keys = {OPENAI_PRIMARY, OPENAI_BACKUP}
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "model": "gpt-4o"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 502, r.text
    assert fake_anthropic.calls == []
    assert [c["api_key"] for c in fake_openai.calls] == [OPENAI_PRIMARY, OPENAI_BACKUP]


@pytest.mark.asyncio
async def test_openai_400_does_not_rotate_or_cross_over(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_openai: FakeOpenAIClient,
    restore_failover_settings: None,
) -> None:
    _enable_openai_failover(crossover=True)
    bad = UpstreamError("openai upstream error")
    bad.__cause__ = _Status(
        400,
        '{"error":{"message":"Invalid input: expected a string","type":"invalid_request_error"}}',
    )
    fake_openai.errors = [bad]
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits", "model": "gpt-4o"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 502, r.text
    assert len(fake_openai.calls) == 1
    assert fake_anthropic.calls == []


@pytest.mark.asyncio
async def test_anthropic_upstream_failure_skips_backup_straight_to_openai(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_openai: FakeOpenAIClient,
    restore_failover_settings: None,
) -> None:
    s = get_settings()
    s.llm_provider = "anthropic"
    s.llm_providers_raw = ""
    s.anthropic_api_key = ANTHROPIC_PRIMARY
    s.anthropic_api_key_backup = ANTHROPIC_BACKUP
    s.openai_api_key = OPENAI_PRIMARY
    s.openai_api_key_backup = OPENAI_BACKUP
    s.anthropic_model = "claude-sonnet-4-5"
    s.openai_model = "gpt-4o"
    s.anthropic_models_raw = json.dumps({"claude-sonnet-4-5": "Sonnet"})
    s.openai_models_raw = json.dumps({"gpt-4o": "GPT-4o"})
    s.anthropic_chat_fallback_openai_model = "gpt-4o"
    s.openai_chat_fallback_anthropic_model = ""

    fake_anthropic.raise_upstream = True
    fake_openai.responses = [fake_openai.text_result("openai covered")]
    async with db_sessionmaker() as sdb:
        uid = await seed_user(sdb, subscription="active", balance=5)

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
    assert r.status_code == 200, r.text
    assert r.json()["assistantMessage"] == "openai covered"
    assert len(fake_anthropic.calls) == 1
    assert fake_anthropic.calls[0]["api_key"] == ANTHROPIC_PRIMARY
    assert [c["api_key"] for c in fake_openai.calls] == [OPENAI_PRIMARY]
    assert fake_openai.calls[-1]["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_byok_does_not_rotate_to_service_backup(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    restore_failover_settings: None,
) -> None:
    s = get_settings()
    s.anthropic_api_key_backup = ANTHROPIC_BACKUP
    s.anthropic_chat_fallback_openai_model = "gpt-4o"
    async with db_sessionmaker() as db:
        uid = await seed_user(
            db, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    fake_anthropic.auth_error_keys = {"sk-ant-user-key"}

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "byok"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "blocked"
    assert r.json()["blockReason"] == "byok_invalid"
    assert [c["api_key"] for c in fake_anthropic.calls] == ["sk-ant-user-key"]
