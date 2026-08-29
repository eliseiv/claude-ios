"""Инструменты работы с кодом (ADR-094): регистрация, гейт и признак подтверждения.

Проверяется не «инструмент существует», а три вещи, каждая из которых ломается молча:

1. ПОЛНОТА РЕГИСТРАЦИИ. Инструмент объявляется в пяти местах сразу. Пропуск любого не виден
   при чтении кода: без имени в таблице Anthropic провайдер отвечает 400 (наружу 502), без
   модели аргументов вызов не разбирается, без описания модель не понимает, зачем инструмент.
2. ГЕЙТ. Инструменты правят файлы на машине человека; на инстансе, где клиент их не умеет,
   модель звала бы их впустую и ход оставался бы незавершённым.
3. ПОДТВЕРЖДЕНИЕ. Список опасных вызовов задаёт сервер. Если клиент начнёт выводить его из
   имени, он разойдётся с бэкендом на следующем добавленном инструменте — и исполнит без
   спроса то, что спрашивать полагалось.
"""

from __future__ import annotations

import pytest

from app.chat.tools import (
    ALL_TOOL_NAMES,
    CODE_TOOLS,
    CONFIRM_TOOLS,
    MUTATING_TOOLS,
    TOOL_DESCRIPTIONS,
    TOOL_FILES_PATCH,
    TOOL_FILES_SEARCH,
    TOOL_GIT_COMMIT,
    TOOL_GIT_DIFF,
    TOOL_GIT_PUSH,
    TOOL_GIT_STATUS,
    anthropic_tool_definitions,
    neutral_tool_definitions,
    openai_tool_definitions,
    to_anthropic_tool_name,
    to_domain_tool_name,
    tool_input_schema,
    validate_tool_args,
)


@pytest.mark.parametrize("name", sorted(CODE_TOOLS))
def test_code_tool_is_registered_everywhere(name: str) -> None:
    assert name in ALL_TOOL_NAMES, "инструмент не попал в общий перечень"
    assert name in TOOL_DESCRIPTIONS, "без описания модель не поймёт назначение"
    assert TOOL_DESCRIPTIONS[name].strip(), "пустое описание бесполезно"
    schema = tool_input_schema(name)
    assert schema.get("type") == "object", "схема аргументов не построилась"
    anthropic = to_anthropic_tool_name(name)
    assert "." not in anthropic, "точка в имени: Anthropic ответит 400, наружу уйдёт 502"


def test_code_tools_hidden_unless_enabled() -> None:
    """По умолчанию их нет: клиент, не умеющий их исполнять, оставил бы ход незавершённым."""
    off = {d["name"] for d in neutral_tool_definitions(code_tools_enabled=False)}
    assert not (off & CODE_TOOLS), "инструменты кода предложены при выключенном флаге"

    on = {d["name"] for d in neutral_tool_definitions(code_tools_enabled=True)}
    assert on >= CODE_TOOLS, "при включённом флаге предложены не все"


def test_reading_needs_no_confirmation_but_writing_does() -> None:
    """Диалог на каждый просмотр файла приучил бы нажимать «да» не глядя."""
    for name in (TOOL_FILES_SEARCH, TOOL_GIT_STATUS, TOOL_GIT_DIFF):
        assert name not in CONFIRM_TOOLS, f"{name} только читает"
    for name in (TOOL_FILES_PATCH, TOOL_GIT_COMMIT, TOOL_GIT_PUSH):
        assert name in CONFIRM_TOOLS, f"{name} меняет состояние и требует согласия"


def test_every_mutating_code_tool_is_also_confirmed() -> None:
    """Изменяющий, но не подтверждаемый инструмент исполнился бы без ведома человека."""
    unconfirmed = (CODE_TOOLS & MUTATING_TOOLS) - CONFIRM_TOOLS
    assert not unconfirmed, f"меняют состояние без подтверждения: {sorted(unconfirmed)}"


def test_code_paths_allow_absolute_and_parent_traversal() -> None:
    """Ограничение каталогом снято владельцем: помощник обязан ходить по всему проекту.

    files.* остаются в песочнице и `..` по-прежнему не принимают — это разные контракты,
    и тест закрепляет, что послабление НЕ протекло на них.
    """
    args = validate_tool_args(
        TOOL_FILES_SEARCH, {"path": "/Users/me/proj/../other", "query": "def"}
    )
    assert args["path"].endswith("other")

    with pytest.raises(ValueError, match="traversal"):
        validate_tool_args("files.read", {"path": "../../etc/passwd"})


def test_push_force_is_a_separate_field() -> None:
    """`force` — отдельное поле, чтобы приложение показало его человеку в диалоге.

    Спрятанный внутри строки флаг человек бы не увидел, а «отправить» и «отправить с
    перезаписью истории» — разные по последствиям действия.
    """
    assert validate_tool_args(TOOL_GIT_PUSH, {"path": "/repo", "force": True})["force"] is True
    assert validate_tool_args(TOOL_GIT_PUSH, {"path": "/repo"})["force"] is False


@pytest.mark.parametrize("enabled", [False, True])
def test_axis_d_reaches_every_provider_serializer(enabled: bool) -> None:
    """Ось D обязана действовать одинаково у ВСЕХ трёх сериализаторов, а не только у neutral.

    Реальный дефект, пойманный этим тестом: `openai_tool_definitions` принимал
    `code_tools_enabled`, но не пробрасывал его в `neutral_tool_definitions`. Отказ молчаливый —
    параметр в сигнатуре есть, вызывающий код выглядит правильным, — а на OpenAI-инстансе
    (именно такой у целевого `fanappsnew`) инструменты не появились бы вовсе.
    """
    neutral = {d["name"] for d in neutral_tool_definitions(code_tools_enabled=enabled)}
    # Оба провайдерских сериализатора несут WIRE-имена (с подчёркиванием) — Anthropic отклоняет
    # точку в `tool.name` (400 → наружу 502). Сравниваем в доменных именах.
    openai = {
        to_domain_tool_name(d["function"]["name"])
        for d in openai_tool_definitions(code_tools_enabled=enabled)
    }
    anthropic = {
        to_domain_tool_name(d["name"])
        for d in anthropic_tool_definitions(code_tools_enabled=enabled)
    }
    assert neutral == openai == anthropic, "сериализаторы расходятся по составу"
    present = neutral & CODE_TOOLS
    assert present == (CODE_TOOLS if enabled else set())
