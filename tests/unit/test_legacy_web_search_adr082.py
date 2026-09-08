"""Unit: CHAT_LEGACY_WEB_SEARCH_ENABLED lifts legacy /v1/chat/run to research (ADR-082)."""

from __future__ import annotations

import pytest

from app.chat.llm_client import generation_llm_client_for, llm_client_for
from app.chat.openai_client import OpenAIClient
from app.chat.openai_responses_client import OpenAIResponsesClient
from app.chat.orchestrator import (
    _credits_llm,
    _effective_generation_mode,
    _turn_credit_cost,
    _uses_generation_client,
)
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


def test_turn_credit_cost_legacy_gets_more_expensive_when_general_is_above_one() -> None:
    """ADR-099 §5.3, последняя строка таблицы: легаси-ход ДОРОЖАЕТ, а не дешевеет.

    Сегодня легаси-путь возвращал литерал `1` и `CHAT_CREDIT_COST_GENERAL` не читал вовсе.
    Перевод на общий резолвер (обязательный — иначе появился бы второй механизм цены, ADR-064 §9)
    делает ход равным цене модели, дефолт которой = `CHAT_CREDIT_COST_GENERAL`. Правило «после
    выката всё дешевеет» неверно, и кейс закрывает именно эту сторону: он падает при возврате
    литерала `1`, потому что `_GENERAL` выставлен заведомо больше единицы.
    """
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
        assert _turn_credit_cost(None) == 9
    finally:
        (
            settings.chat_legacy_web_search_enabled,
            settings.chat_credit_cost_general,
            settings.chat_credit_cost_research,
        ) = original


def test_turn_credit_cost_ignores_the_legacy_flag_entirely() -> None:
    """Флаг переводит ход в `research` ПО ПОВЕДЕНИЮ (hosted-поиск), но не по цене.

    Кейс падает, если списание снова начнёт зависеть от `CHAT_CREDIT_COST_RESEARCH`: значение
    выставлено отличным от `_GENERAL` намеренно.
    """
    settings = get_settings()
    original = (
        settings.chat_legacy_web_search_enabled,
        settings.chat_credit_cost_general,
        settings.chat_credit_cost_research,
    )
    settings.chat_credit_cost_general = 2
    settings.chat_credit_cost_research = 3
    try:
        settings.chat_legacy_web_search_enabled = True
        with_flag = _turn_credit_cost(None)
        settings.chat_legacy_web_search_enabled = False
        without_flag = _turn_credit_cost(None)
        assert with_flag == without_flag == 2
    finally:
        (
            settings.chat_legacy_web_search_enabled,
            settings.chat_credit_cost_general,
            settings.chat_credit_cost_research,
        ) = original


def test_legacy_flag_uses_openai_responses_not_completions() -> None:
    settings = get_settings()
    original = settings.chat_legacy_web_search_enabled
    settings.chat_legacy_web_search_enabled = False
    try:
        assert _uses_generation_client(False) is False
        assert isinstance(_credits_llm(provider="openai", use_generation_v2=False), OpenAIClient)
        settings.chat_legacy_web_search_enabled = True
        assert _uses_generation_client(False) is True
        assert isinstance(
            _credits_llm(provider="openai", use_generation_v2=False), OpenAIResponsesClient
        )
        # Anthropic has one Messages client; the flag must not fork it.
        assert _credits_llm(provider="anthropic", use_generation_v2=False) is llm_client_for(
            "anthropic"
        )
        assert _credits_llm(
            provider="anthropic", use_generation_v2=False
        ) is generation_llm_client_for("anthropic")
    finally:
        settings.chat_legacy_web_search_enabled = original
