"""Unit: реестр персонажей и слой системного промта (ADR-097)."""

from __future__ import annotations

import pytest

from app.chat.characters import (
    character_catalog,
    character_prompt_layer,
    is_known_character,
)
from app.chat.orchestrator import _system_prompt_for, _system_prompt_with_workspace
from app.config import get_settings

EXPECTED_IDS = ["anime_girl", "fantasy_queen", "vampire_lord", "cyber_assassin", "virtual_friend"]


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_catalog_has_exactly_the_five_characters_in_order() -> None:
    assert [c["id"] for c in character_catalog("en")] == EXPECTED_IDS


def test_catalog_never_exposes_persona() -> None:
    # Промт персонажа — инструкция модели, а не отображаемый текст: отдав его наружу, мы
    # заморозили бы формулировку в контракте приложения (ADR-097 §3).
    for locale in ("en", "ru", "zh-Hans"):
        for row in character_catalog(locale):
            assert set(row) == {"id", "name", "tagline", "icon"}


def test_unfilled_locale_falls_back_to_english_per_field() -> None:
    en = {c["id"]: c for c in character_catalog("en")}
    zh = {c["id"]: c for c in character_catalog("zh-Hans")}
    ru = {c["id"]: c for c in character_catalog("ru")}
    for cid in EXPECTED_IDS:
        # Незаполненная локаль отдаёт канон, а НЕ пустую строку.
        assert zh[cid]["name"] == en[cid]["name"]
        assert zh[cid]["tagline"] == en[cid]["tagline"]
        assert ru[cid]["name"] and ru[cid]["tagline"]
        # id и иконка от локали не зависят.
        assert ru[cid]["icon"] == en[cid]["icon"]


def test_known_character_is_exact_match() -> None:
    assert is_known_character("anime_girl") is True
    assert is_known_character("Anime_Girl") is False
    assert is_known_character("") is False
    assert is_known_character("no_such_character") is False


def test_prompt_layer_absent_without_character_and_for_retired_id() -> None:
    assert character_prompt_layer(None) is None
    assert character_prompt_layer("") is None
    # Персонаж, убранный из реестра: ход обязан продолжиться обычным голосом, а не упасть.
    assert character_prompt_layer("retired_character") is None


def test_prompt_layer_carries_persona_and_guardrails() -> None:
    layer = character_prompt_layer("vampire_lord")
    assert layer is not None
    assert "Character rules" in layer


def test_character_layer_sits_between_base_and_mode_suffix(monkeypatch) -> None:
    monkeypatch.setenv("CHARACTERS_ENABLED", "true")
    get_settings.cache_clear()
    plain = _system_prompt_for("general", "study_learn")
    withc = _system_prompt_for("general", "study_learn", "anime_girl")
    layer = character_prompt_layer("anime_girl")
    assert layer is not None and layer in withc
    # Порядок нормативен: задача хода обязана перебивать декоративный тон, поэтому суффикс
    # режима идёт ПОСЛЕ персонажа. Тест обязан падать при перестановке слоёв.
    suffix = withc.split(layer)[1]
    assert suffix.strip(), "после персонажа обязан идти суффикс режима"
    assert suffix.strip() in plain


def test_flag_off_means_no_layer_at_all(monkeypatch) -> None:
    monkeypatch.setenv("CHARACTERS_ENABLED", "false")
    get_settings.cache_clear()
    # Даже у сессии с сохранённым персонажем: флаг снят — поведение прежнее до байта.
    assert _system_prompt_for("general", "general", "anime_girl") == _system_prompt_for(
        "general", "general"
    )


def test_workspace_instructions_come_after_the_character(monkeypatch) -> None:
    monkeypatch.setenv("CHARACTERS_ENABLED", "true")
    get_settings.cache_clear()
    prompt = _system_prompt_with_workspace(
        assistant_mode="general",
        generation_mode="general",
        instructions="Всегда отвечай списком.",
        character_id="fantasy_queen",
    )
    layer = character_prompt_layer("fantasy_queen")
    assert layer is not None
    # Пробел ловится только сверкой ПОРЯДКА: инструкции пользователя весомее образа.
    assert layer in prompt
    assert prompt.index(layer) < prompt.index("Всегда отвечай списком.")
