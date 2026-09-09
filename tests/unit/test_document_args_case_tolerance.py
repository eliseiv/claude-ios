"""Модель пишет `mediatype`, схема ждёт `mediaType` — и ход падал молча.

Прод 2026-09-09, avelyra, пользователь 074a4d01: ШЕСТЬ отказов `document.create` из семи попыток,
у всех один текст — `{"code": "invalid_document_request", "message": "mediatype: extra_forbidden"}`.
Удалась ровно та попытка, где модель НЕ прислала тип вовсе и сработал дефолт `text/markdown`.
Со стороны человека это выглядело как «умеет в md, а в txt не хочет».

Кейсы разделены по инвариантам, и каждый падает при откате СВОЕЙ части:
приведение имени ключа, сохранённая строгость к неизвестным ключам, внятный текст отказа,
наличие строки про документы в системном промте.
"""

from __future__ import annotations

import pytest

from app.chat.orchestrator import _system_prompt_for
from app.chat.tools import (
    DOCUMENT_FIELDS_HINT,
    TOOL_DOCUMENT_CREATE,
    TOOL_DOCUMENT_READ,
    TOOL_DOCUMENT_UPDATE,
    content_free_args_error,
    validate_tool_args,
)

# ------------------------------- приведение имени ключа -------------------------------


@pytest.mark.parametrize("spelling", ["mediatype", "media_type", "MediaType", "MEDIATYPE"])
def test_media_type_key_is_accepted_in_any_case(spelling: str) -> None:
    out = validate_tool_args(TOOL_DOCUMENT_CREATE, {spelling: "text/plain", "content": "x"})
    assert out["mediaType"] == "text/plain"
    assert "content" in out


def test_document_update_and_read_accept_case_variants() -> None:
    upd = validate_tool_args(TOOL_DOCUMENT_UPDATE, {"documentid": "abc", "content": "x"})
    assert upd["documentId"] == "abc"
    read = validate_tool_args(TOOL_DOCUMENT_READ, {"document_id": "abc"})
    assert read["documentId"] == "abc"


def test_canonical_spelling_still_works() -> None:
    out = validate_tool_args(TOOL_DOCUMENT_CREATE, {"mediaType": "text/csv", "content": "a,b"})
    assert out["mediaType"] == "text/csv"


# --------------------------- строгость к НЕИЗВЕСТНЫМ ключам сохранена ---------------------------


def test_unknown_key_is_still_rejected() -> None:
    """Терпимость к регистру не превращает строгую схему в свободную."""
    with pytest.raises(ValueError):
        validate_tool_args(TOOL_DOCUMENT_CREATE, {"totallyUnknown": "x"})


def test_both_spellings_together_are_rejected_not_silently_merged() -> None:
    """Выбрать одно из двух значений молча значило бы угадать за модель."""
    with pytest.raises(ValueError):
        validate_tool_args(
            TOOL_DOCUMENT_CREATE,
            {"mediaType": "text/plain", "mediatype": "text/csv", "content": "x"},
        )


# --------------------------------- внятный текст отказа ---------------------------------


def test_unknown_field_reads_as_words_not_pydantic_kind() -> None:
    try:
        validate_tool_args(TOOL_DOCUMENT_CREATE, {"totallyUnknown": "x"})
    except ValueError as exc:  # noqa: PT011 — нужен сам текст, а не тип
        message = content_free_args_error(exc)
    else:  # pragma: no cover — предыдущий кейс гарантирует отказ
        pytest.fail("ожидался отказ схемы")
    assert "unknown field" in message
    assert "extra_forbidden" not in message


def test_hint_names_the_accepted_fields_and_the_camel_case() -> None:
    """Подсказка обязана СКАЗАТЬ верное имя — иначе модель тычется вслепую."""
    assert "mediaType" in DOCUMENT_FIELDS_HINT
    assert "mediatype" in DOCUMENT_FIELDS_HINT  # названо и НЕверное написание
    assert "text/plain" in DOCUMENT_FIELDS_HINT


# ------------------------------ строка промта про документы ------------------------------


def test_system_prompt_tells_the_model_to_use_the_document_tool() -> None:
    """Инструкции про документы не было вовсе — в отличие от медиа, кода, карт и режимов."""
    prompt = _system_prompt_for("general")
    assert "document.create" in prompt


def test_document_instruction_is_unconditional_across_modes() -> None:
    for assistant_mode in ("general", "code"):
        for generation_mode in ("general", "research", "study_learn", "reasoning"):
            assert "document.create" in _system_prompt_for(assistant_mode, generation_mode)
