"""Unit: dual-credits Settings helpers (ADR-073).

Opt-in ``LLM_PROVIDERS`` unions catalogs and routes a model id to its provider. Unset/empty
keeps ADR-033 single-provider behaviour even when both API keys are present.
"""

from __future__ import annotations

import json

from app.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_credits_providers_unset_is_only_llm_provider_despite_both_keys() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant-x",
        LLM_PROVIDERS="",
    )
    assert s.credits_providers() == ("openai",)
    assert s.allowed_models_union() == s.allowed_models()


def test_credits_providers_ignores_extra_without_api_key() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="",
        LLM_PROVIDERS="anthropic",
    )
    assert s.credits_providers() == ("openai",)


def test_credits_providers_opt_in_appends_extra_with_key() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant-x",
        LLM_PROVIDERS="openai,anthropic",
    )
    assert s.credits_providers() == ("openai", "anthropic")


def test_allowed_models_union_merges_both_allowlists() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant-x",
        LLM_PROVIDERS="anthropic",
        OPENAI_MODEL="gpt-4o",
        ANTHROPIC_MODEL="claude-sonnet-4-5",
        OPENAI_MODELS=json.dumps({"gpt-4o": "GPT-4o", "gpt-4o-mini": "GPT-4o mini"}),
        ANTHROPIC_MODELS=json.dumps({"claude-sonnet-4-5": "Sonnet", "claude-opus": "Opus"}),
    )
    union = s.allowed_models_union()
    assert list(union)[:2] == ["gpt-4o", "gpt-4o-mini"]
    assert "claude-sonnet-4-5" in union
    assert "claude-opus" in union
    # Single-provider allowed_models() is unchanged (openai only).
    assert "claude-opus" not in s.allowed_models()


def test_credits_provider_for_model_routes_by_allowlist() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant-x",
        LLM_PROVIDERS="anthropic",
        OPENAI_MODEL="gpt-4o",
        ANTHROPIC_MODEL="claude-sonnet-4-5",
        OPENAI_MODELS=json.dumps({"gpt-4o": "GPT-4o"}),
        ANTHROPIC_MODELS=json.dumps({"claude-sonnet-4-5": "Sonnet"}),
    )
    assert s.credits_provider_for_model(None) == "openai"
    assert s.credits_provider_for_model("gpt-4o") == "openai"
    assert s.credits_provider_for_model("claude-sonnet-4-5") == "anthropic"
    # Stale / unknown id → instance default provider (stale-model guard, not 502).
    assert s.credits_provider_for_model("totally-unknown") == "openai"


def test_catalog_models_default_first_and_additive_provider() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant-x",
        LLM_PROVIDERS="anthropic",
        OPENAI_MODEL="gpt-4o",
        ANTHROPIC_MODEL="claude-sonnet-4-5",
        OPENAI_MODELS=json.dumps({"gpt-4o": "GPT-4o", "gpt-4o-mini": "Mini"}),
        ANTHROPIC_MODELS=json.dumps({"claude-sonnet-4-5": "Sonnet"}),
    )
    rows = s.catalog_models()
    assert rows[0] == ("gpt-4o", "GPT-4o", True, "openai")
    assert ("gpt-4o-mini", "Mini", False, "openai") in rows
    assert ("claude-sonnet-4-5", "Sonnet", False, "anthropic") in rows
    assert sum(1 for _id, _n, is_default, _p in rows if is_default) == 1


def test_catalog_models_single_provider_only_emits_active() -> None:
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai",
        ANTHROPIC_API_KEY="sk-ant-x",
        LLM_PROVIDERS="",
        OPENAI_MODEL="gpt-4o",
        ANTHROPIC_MODEL="claude-sonnet-4-5",
        OPENAI_MODELS=json.dumps({"gpt-4o": "GPT-4o"}),
        ANTHROPIC_MODELS=json.dumps({"claude-sonnet-4-5": "Sonnet"}),
    )
    rows = s.catalog_models()
    assert [r[0] for r in rows] == ["gpt-4o"]
    assert rows[0][3] == "openai"
