"""Unit: Settings.allowed_models() / default_model() — model allowlist (ADR-034 §1 / ADR-076).

Pure config logic (no I/O). Settings is constructed directly with alias kwargs (same pattern as
test_attachments.py / test_billing_adapty_parser.py) so each case is hermetic and independent of
the process env. Covers:
- empty allowlist → instance default first + built-in product catalog (ADR-076);
- env allowlist ADDS extras and may override display names (does not hide built-in rows);
- default always present (prepended when missing from env/builtin);
- shape rules (token_products parity): non-str values / blank keys / non-object / bad JSON dropped;
- provider selection: anthropic vs openai raw chosen by LLM_PROVIDER, default_model() per provider.
"""

from __future__ import annotations

import json

from app.chat.product_catalog import ANTHROPIC_PRODUCT_MODELS, OPENAI_PRODUCT_MODELS
from app.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _assert_default_then_product(result: dict[str, str], *, default: str, openai: bool) -> None:
    builtin = OPENAI_PRODUCT_MODELS if openai else ANTHROPIC_PRODUCT_MODELS
    assert list(result)[0] == default
    for model_id in builtin:
        assert model_id in result


# --------------------------- default_model() per provider ---------------------------
def test_default_model_anthropic_is_anthropic_model() -> None:
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5")
    assert s.default_model() == "claude-sonnet-4-5"


def test_default_model_openai_is_openai_model() -> None:
    s = _settings(LLM_PROVIDER="openai", OPENAI_MODEL="gpt-4o")
    assert s.default_model() == "gpt-4o"


def test_default_model_provider_case_insensitive() -> None:
    s = _settings(LLM_PROVIDER="OpenAI", OPENAI_MODEL="gpt-4o", ANTHROPIC_MODEL="claude-x")
    assert s.default_model() == "gpt-4o"


# --------------------------- empty allowlist → default + product catalog -------------------
def test_empty_allowlist_includes_anthropic_product_catalog() -> None:
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5")
    result = s.allowed_models()
    _assert_default_then_product(result, default="claude-sonnet-4-5", openai=False)
    assert result["claude-sonnet-4-5"] == "Claude Sonnet 4.5"
    assert result["claude-opus-5"] == "Claude Opus 5"
    assert result["claude-haiku-4-5-20251001"] == "Claude Haiku 4.5"
    assert "gpt-5.1" not in result


def test_empty_allowlist_includes_openai_product_catalog() -> None:
    s = _settings(LLM_PROVIDER="openai", OPENAI_MODEL="gpt-4o", OPENAI_MODELS="{}")
    result = s.allowed_models()
    _assert_default_then_product(result, default="gpt-4o", openai=True)
    assert result["gpt-4o"] == "GPT-4o"
    assert result["gpt-5.1"] == "GPT-5.1"
    assert result["gpt-4.1"] == "GPT-4.1"
    assert "claude-opus-5" not in result


def test_default_always_present_in_empty_case() -> None:
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5")
    assert s.default_model() in s.allowed_models()


# --------------------------- env allowlist adds extras / overrides names -------------------
def test_allowlist_without_default_prepends_default_then_product_then_extras() -> None:
    raw = json.dumps({"claude-haiku": "Claude Haiku", "claude-opus": "Claude Opus"})
    s = _settings(
        LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5", ANTHROPIC_MODELS=raw
    )
    result = s.allowed_models()
    _assert_default_then_product(result, default="claude-sonnet-4-5", openai=False)
    assert result["claude-haiku"] == "Claude Haiku"
    assert result["claude-opus"] == "Claude Opus"
    assert list(result).index("claude-haiku") > list(result).index("claude-opus-5")


def test_allowlist_overrides_builtin_display_name() -> None:
    raw = json.dumps({"claude-sonnet-4-5": "Sonnet", "claude-haiku": "Claude Haiku"})
    s = _settings(
        LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5", ANTHROPIC_MODELS=raw
    )
    result = s.allowed_models()
    assert result["claude-sonnet-4-5"] == "Sonnet"
    assert result["claude-opus-5"] == "Claude Opus 5"
    assert result["claude-haiku"] == "Claude Haiku"


# --------------------------- shape rules (token_products parity) ---------------------------
def test_non_str_values_are_dropped() -> None:
    raw = json.dumps(
        {
            "good": "Good Model",
            "as_int": 5,
            "as_null": None,
            "as_bool": True,
            "as_obj": {"x": 1},
            "empty_val": "",
        }
    )
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-def", ANTHROPIC_MODELS=raw)
    result = s.allowed_models()
    assert list(result)[0] == "claude-def"
    assert result["claude-def"] == "claude-def"
    assert result["good"] == "Good Model"
    assert "as_int" not in result
    _assert_default_then_product(result, default="claude-def", openai=False)


def test_blank_keys_are_dropped_and_keys_stripped() -> None:
    raw = json.dumps({"   ": "blank-key", "  spaced  ": "Spaced"})
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-def", ANTHROPIC_MODELS=raw)
    result = s.allowed_models()
    assert "   " not in result
    assert result["spaced"] == "Spaced"


def test_malformed_json_keeps_product_catalog() -> None:
    s = _settings(
        LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-def", ANTHROPIC_MODELS="not-json{{{"
    )
    result = s.allowed_models()
    assert result["claude-def"] == "claude-def"
    _assert_default_then_product(result, default="claude-def", openai=False)


def test_non_object_json_keeps_product_catalog() -> None:
    s = _settings(
        LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-def", ANTHROPIC_MODELS='["a","b"]'
    )
    result = s.allowed_models()
    assert result["claude-def"] == "claude-def"
    _assert_default_then_product(result, default="claude-def", openai=False)


# --------------------------- provider selection of the raw allowlist ---------------------------
def test_provider_selects_anthropic_raw_when_anthropic() -> None:
    anth = json.dumps({"claude-x": "Claude X"})
    open = json.dumps({"gpt-x": "GPT X"})
    s = _settings(
        LLM_PROVIDER="anthropic",
        ANTHROPIC_MODEL="claude-def",
        ANTHROPIC_MODELS=anth,
        OPENAI_MODELS=open,
    )
    result = s.allowed_models()
    assert "claude-x" in result
    assert "gpt-x" not in result
    assert "gpt-5.1" not in result


def test_provider_selects_openai_raw_when_openai() -> None:
    anth = json.dumps({"claude-x": "Claude X"})
    open = json.dumps({"gpt-x": "GPT X"})
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_MODEL="gpt-def",
        ANTHROPIC_MODELS=anth,
        OPENAI_MODELS=open,
    )
    result = s.allowed_models()
    assert "gpt-x" in result
    assert "claude-x" not in result
    assert s.default_model() == "gpt-def"
    assert s.default_model() in result
    _assert_default_then_product(result, default="gpt-def", openai=True)
