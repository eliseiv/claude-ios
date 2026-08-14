"""Unit: unified instance catalog (ADR-075).

Chat rows follow credits_providers() (leftover opposite key does not add a provider).
Fal rows appear only when FAL_API_KEY is non-empty, one row per registry endpoint.
"""

from __future__ import annotations

import json

from app.chat.instance_catalog import build_instance_catalog
from app.config import Settings
from app.media_generation.catalog import DEFAULT_PHOTO_ENDPOINT, fal_catalog_entries


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_fal_catalog_covers_registry_endpoints() -> None:
    entries = fal_catalog_entries()
    ids = [e.id for e in entries]
    assert len(ids) == len(set(ids))
    assert DEFAULT_PHOTO_ENDPOINT in ids
    defaults = [e for e in entries if e.default]
    assert [e.id for e in defaults] == [DEFAULT_PHOTO_ENDPOINT]
    assert all(e.modality in {"photo", "video"} for e in entries)
    assert all(e.variant and e.family for e in entries)


def test_catalog_without_fal_key_is_chat_only() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant-leftover",
        LLM_PROVIDERS="",
        OPENAI_MODEL="gpt-4o",
        OPENAI_MODELS=json.dumps({"gpt-4o": "GPT-4o"}),
        FAL_API_KEY="",
    )
    rows = build_instance_catalog(s)
    assert rows[0].id == "gpt-4o"
    assert "gpt-5.1" in [m.id for m in rows]
    assert all(m.provider == "openai" for m in rows)
    assert rows[0].name == "GPT-4o"
    assert rows[0].displayName == "GPT-4o"
    assert rows[0].modality == "chat"
    assert rows[0].variant is None
    assert rows[0].family is None
    assert rows[0].provider == "openai"


def test_catalog_with_fal_key_appends_photo_and_video() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        OPENAI_MODEL="gpt-4o",
        OPENAI_MODELS=json.dumps({"gpt-4o": "GPT-4o"}),
        FAL_API_KEY="fal-key",
    )
    rows = build_instance_catalog(s)
    assert rows[0].id == "gpt-4o"
    assert rows[0].default is True
    assert rows[0].modality == "chat"
    fal = [m for m in rows if m.provider == "fal"]
    assert {m.modality for m in fal} == {"photo", "video"}
    assert any(m.id == "fal-ai/nano-banana-pro" and m.default for m in fal)
    assert any(m.id == "fal-ai/nano-banana-2/edit" and m.variant == "Image Editing" for m in fal)


def test_dual_credits_plus_fal_keeps_chat_default_first() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant-x",
        LLM_PROVIDERS="anthropic",
        OPENAI_MODEL="gpt-4o",
        ANTHROPIC_MODEL="claude-sonnet-4-5",
        OPENAI_MODELS=json.dumps({"gpt-4o": "GPT-4o"}),
        ANTHROPIC_MODELS=json.dumps({"claude-sonnet-4-5": "Sonnet"}),
        FAL_API_KEY="fal-key",
    )
    rows = build_instance_catalog(s)
    chat = [m for m in rows if m.modality == "chat"]
    assert chat[0].id == "gpt-4o"
    assert chat[0].default is True
    assert {m.id for m in chat} >= {"gpt-4o", "gpt-5.1", "claude-sonnet-4-5", "claude-opus-5"}
    by_id = {m.id: m for m in chat}
    assert by_id["claude-sonnet-4-5"].provider == "anthropic"
    assert by_id["claude-sonnet-4-5"].default is False
    assert rows[0].id == "gpt-4o"
