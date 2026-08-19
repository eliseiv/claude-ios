"""Unit: CHAT_LEGACY_WEB_SEARCH_ENABLED lifts legacy /v1/chat/run to research (ADR-082)."""

from __future__ import annotations

import pytest

from app.chat.orchestrator import _effective_generation_mode, _turn_credit_cost
from app.config import Settings, get_settings


def test_settings_default_keeps_legacy_without_web_search() -> None:
    get_settings.cache_clear()
    assert Settings().chat_legacy_web_search_enabled is False  # type: ignore[call-arg]
    get_settings.cache_clear()


def test_settings_parses_true(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_LEGACY_WEB_SEARCH_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().chat_legacy_web_search_enabled is True
    get_settings.cache_clear()


def test_effective_mode_legacy_default_is_general() -> None:
    settings = get_settings()
    original = settings.chat_legacy_web_search_enabled
    settings.chat_legacy_web_search_enabled = False
    try:
        assert _effective_generation_mode("research", use_generation_v2=False) == "general"
        assert _effective_generation_mode("general", use_generation_v2=False) == "general"
    finally:
        settings.chat_legacy_web_search_enabled = original


def test_effective_mode_legacy_flag_is_research() -> None:
    settings = get_settings()
    original = settings.chat_legacy_web_search_enabled
    settings.chat_legacy_web_search_enabled = True
    try:
        assert _effective_generation_mode("general", use_generation_v2=False) == "research"
    finally:
        settings.chat_legacy_web_search_enabled = original


def test_effective_mode_v2_ignores_legacy_flag() -> None:
    settings = get_settings()
    original = settings.chat_legacy_web_search_enabled
    settings.chat_legacy_web_search_enabled = True
    try:
        assert _effective_generation_mode("general", use_generation_v2=True) == "general"
        assert _effective_generation_mode("reasoning", use_generation_v2=True) == "reasoning"
    finally:
        settings.chat_legacy_web_search_enabled = original


def test_turn_credit_cost_legacy_default_stays_one() -> None:
    settings = get_settings()
    original = (
        settings.chat_legacy_web_search_enabled,
        settings.chat_credit_cost_general,
        settings.chat_credit_cost_research,
    )
    settings.chat_legacy_web_search_enabled = False
    settings.chat_credit_cost_general = 9
    settings.chat_credit_cost_research = 3
    try:
        assert _turn_credit_cost("general", use_generation_v2=False) == 1
        assert _turn_credit_cost("research", use_generation_v2=False) == 1
    finally:
        (
            settings.chat_legacy_web_search_enabled,
            settings.chat_credit_cost_general,
            settings.chat_credit_cost_research,
        ) = original


def test_turn_credit_cost_legacy_flag_uses_research_price() -> None:
    settings = get_settings()
    original = (
        settings.chat_legacy_web_search_enabled,
        settings.chat_credit_cost_research,
    )
    settings.chat_legacy_web_search_enabled = True
    settings.chat_credit_cost_research = 3
    try:
        assert _turn_credit_cost("research", use_generation_v2=False) == 3
    finally:
        (
            settings.chat_legacy_web_search_enabled,
            settings.chat_credit_cost_research,
        ) = original
