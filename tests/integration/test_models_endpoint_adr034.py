"""Integration: GET /v1/models (ADR-034 §2).

JWT-protected like GET /v1/tools. Uses the shared hermetic `client` (real PG container, faked
external clients, rate limits fail open without Redis). Covers:
- 401 without a JWT / with a broken bearer;
- with a JWT: the active provider's allowlist, EXACTLY one default:true, default FIRST;
- empty allowlist → instance default first + built-in product catalog (ADR-076);
- env allowlist adds extras / overrides names; default still first;
- 429 when the per-user read limiter rejects.

The allowlist is configured by mutating the process-wide cached Settings instance (same approach as
test_adr022_project_gating mutating settings.preview_url_secret), restored after each test.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user


@pytest.fixture
def restore_model_settings() -> Iterator[None]:
    """Snapshot/restore the model-allowlist Settings fields (the cached singleton is mutated)."""
    s = get_settings()
    orig = (
        s.llm_provider,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.llm_providers_raw,
        s.openai_api_key,
        s.anthropic_api_key,
        s.openai_model,
        s.fal_api_key,
    )
    yield
    (
        s.llm_provider,
        s.anthropic_models_raw,
        s.openai_models_raw,
        s.anthropic_model,
        s.llm_providers_raw,
        s.openai_api_key,
        s.anthropic_api_key,
        s.openai_model,
        s.fal_api_key,
    ) = orig


def _set_allowlist(*, provider: str, anthropic_raw: str, anthropic_model: str) -> None:
    s = get_settings()
    s.llm_provider = provider
    s.anthropic_models_raw = anthropic_raw
    s.anthropic_model = anthropic_model
    s.fal_api_key = ""


# ----------------------------- auth gate -----------------------------
@pytest.mark.asyncio
async def test_models_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/models")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_models_broken_bearer_401(client: AsyncClient) -> None:
    r = await client.get("/v1/models", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


# ----------------------------- empty allowlist → default + product catalog -------------------
@pytest.mark.asyncio
async def test_models_empty_allowlist_includes_product_catalog(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    _set_allowlist(provider="anthropic", anthropic_raw="{}", anthropic_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    ids = [m["id"] for m in models]
    assert ids[0] == "claude-sonnet-4-5"
    assert models[0]["default"] is True
    assert models[0]["displayName"] == "Claude Sonnet 4.5"
    assert models[0]["name"] == "Claude Sonnet 4.5"
    assert "claude-opus-5" in ids
    assert "claude-fable-5" in ids
    assert "claude-haiku-4-5-20251001" in ids
    assert all(m["provider"] == "anthropic" for m in models)
    assert all(m["modality"] == "chat" for m in models)


# ----------------------------- non-empty WITHOUT default → default prepended -----------------
@pytest.mark.asyncio
async def test_models_allowlist_without_default_prepends_default_first(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    raw = json.dumps({"claude-haiku": "Claude Haiku", "claude-opus": "Claude Opus"})
    _set_allowlist(provider="anthropic", anthropic_raw=raw, anthropic_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    ids = [m["id"] for m in models]
    assert ids[0] == "claude-sonnet-4-5"
    assert models[0]["default"] is True
    assert models[0]["displayName"] == "Claude Sonnet 4.5"
    assert "claude-haiku" in ids
    assert "claude-opus" in ids
    assert "claude-opus-5" in ids
    assert sum(1 for m in models if m["default"]) == 1


# ----------------------------- non-empty WITH default → order preserved -----------------------
@pytest.mark.asyncio
async def test_models_allowlist_with_default_keeps_display_and_order(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    raw = json.dumps({"claude-sonnet-4-5": "Claude Sonnet 4.5", "claude-haiku": "Claude Haiku"})
    _set_allowlist(provider="anthropic", anthropic_raw=raw, anthropic_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    ids = [m["id"] for m in models]
    assert ids[0] == "claude-sonnet-4-5"
    assert models[0]["default"] is True
    assert "claude-haiku" in ids
    assert "claude-opus-5" in ids
    # displayName from the allowlist is kept for the default (not overwritten with the id).
    assert models[0]["displayName"] == "Claude Sonnet 4.5"
    assert models[0]["name"] == "Claude Sonnet 4.5"
    assert all(m["modality"] == "chat" for m in models)
    assert all(m["variant"] is None and m["family"] is None for m in models)
    assert sum(1 for m in models if m["default"]) == 1


# ----------------------------- exactly one default + default first invariant ------------------
@pytest.mark.asyncio
async def test_models_response_invariants(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    raw = json.dumps({"a": "A", "claude-def": "Default", "b": "B"})
    _set_allowlist(provider="anthropic", anthropic_raw=raw, anthropic_model="claude-def")
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    # default present, exactly one default:true, and it is first.
    defaults = [m for m in models if m["default"]]
    assert len(defaults) == 1
    assert models[0]["default"] is True
    assert models[0]["id"] == "claude-def"
    # additive provider column: single-provider instance → LLM_PROVIDER on every row (ADR-073).
    assert all(m["provider"] == "anthropic" for m in models)
    # no duplicate ids.
    ids = [m["id"] for m in models]
    assert len(ids) == len(set(ids))


# ----------------------------- 429 when limiter rejects -----------------------------
@pytest.mark.asyncio
async def test_models_rate_limited_429(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The models router imported enforce_other_limits by name at module load; patch it there.
    from app.api_gateway.routers import models as models_router

    async def _reject(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(models_router, "enforce_other_limits", _reject)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 429, r.text


# ----------------------------- fal rows when FAL_API_KEY is set (ADR-075) -------------------
@pytest.mark.asyncio
async def test_models_includes_fal_when_key_set(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    _set_allowlist(provider="openai", anthropic_raw="{}", anthropic_model="claude-sonnet-4-5")
    s = get_settings()
    s.llm_provider = "openai"
    s.openai_model = "gpt-4o"
    s.openai_models_raw = json.dumps({"gpt-4o": "GPT-4o"})
    s.openai_api_key = "sk-openai"
    s.anthropic_api_key = "sk-ant-leftover"
    s.llm_providers_raw = ""
    s.fal_api_key = "fal-test-key"
    async with db_sessionmaker() as session:
        uid = await seed_user(session)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    chat = [m for m in models if m["modality"] == "chat"]
    photo = [m for m in models if m["modality"] == "photo"]
    video = [m for m in models if m["modality"] == "video"]
    assert chat[0]["id"] == "gpt-4o"
    assert "gpt-5.1" in [m["id"] for m in chat]
    assert all(m["provider"] == "openai" for m in chat)
    # Leftover Anthropic key without LLM_PROVIDERS does not add Claude (ADR-073).
    assert not any(m["provider"] == "anthropic" for m in models)
    assert {m["id"] for m in photo} >= {
        "fal-ai/nano-banana-pro",
        "fal-ai/nano-banana-pro/edit",
        "fal-ai/nano-banana-2",
        "fal-ai/nano-banana-2/edit",
    }
    assert {m["id"] for m in video} >= {
        "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
        "fal-ai/veo3.1",
    }
    assert all(m["provider"] == "fal" for m in photo + video)
    assert models[0]["id"] == "gpt-4o"
    assert models[0]["default"] is True
    photo_defaults = [m for m in photo if m["default"]]
    assert [m["id"] for m in photo_defaults] == ["fal-ai/nano-banana-pro"]
    assert all(m["default"] is False for m in video)
    pro = next(m for m in photo if m["id"] == "fal-ai/nano-banana-pro")
    assert pro["name"] == "Nano Banana Pro"
    assert pro["displayName"] == "Nano Banana Pro"
    assert pro["variant"] == "Text to Image"
    assert pro["family"] == "Nano-Banana-Pro"


@pytest.mark.asyncio
async def test_models_omits_fal_when_key_empty(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    _set_allowlist(provider="anthropic", anthropic_raw="{}", anthropic_model="claude-sonnet-4-5")
    async with db_sessionmaker() as session:
        uid = await seed_user(session)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    assert all(m["provider"] != "fal" for m in models)
    assert all(m["modality"] == "chat" for m in models)


@pytest.mark.asyncio
async def test_chat_run_rejects_fal_catalog_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    _set_allowlist(provider="openai", anthropic_raw="{}", anthropic_model="claude-sonnet-4-5")
    s = get_settings()
    s.llm_provider = "openai"
    s.openai_model = "gpt-4o"
    s.openai_models_raw = json.dumps({"gpt-4o": "GPT-4o"})
    s.openai_api_key = "sk-openai"
    s.fal_api_key = "fal-test-key"
    async with db_sessionmaker() as session:
        uid = await seed_user(session, subscription="active", balance=5)
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "hi",
            "mode": "credits",
            "model": "fal-ai/nano-banana-pro",
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
