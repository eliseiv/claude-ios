"""Unit: текст отказа `validation_error` голосового кадра называет поле и вид нарушения.

Норма — `docs/modules/chat-orchestrator/02-api-contracts.md`, таблица отказов голосового режима,
строка «Кадр не JSON, неизвестный `type` или схема не сошлась»: `message` называет путь до поля и
вид нарушения; присланные значения не отражаются никогда; имя лишнего поля — только если оно
похоже на идентификатор и не длиннее 64 символов.

Сквозной путь на рабочем сокете — `tests/integration/test_voice_frame_errors.py`; здесь — сама
функция текста на ВСЕХ схемах кадров клиент → сервер, включая то, что на сокете дорого
перебирать (потолок числа нарушений, неотражаемые имена).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

# Заметное значение: если оно окажется в `message`, текст отражает ввод клиента.
SENTINEL = "SENTINEL-7f3a-value"


def _message(model: type[BaseModel], frame_type: str, payload: dict[str, Any]) -> str:
    # Импорт отложен: модуль роутера тянет настройки, а юнит-кейсу окружение не нужно.
    from app.api_gateway.routers.chat_voice import _frame_validation_message

    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)
    return _frame_validation_message(frame_type, caught.value)


def _schemas() -> list[tuple[type[BaseModel], str]]:
    from app.schemas import voice_frames as vf

    return [
        (vf.VoiceStartFrame, vf.FRAME_START),
        (vf.VoiceUtteranceBeginFrame, vf.FRAME_UTTERANCE_BEGIN),
        (vf.VoiceUtteranceEndFrame, vf.FRAME_UTTERANCE_END),
        (vf.VoiceTextFrame, vf.FRAME_TEXT),
        (vf.VoiceInterruptFrame, vf.FRAME_INTERRUPT),
        (vf.VoiceToolResultFrame, vf.FRAME_TOOL_RESULT),
    ]


# ---- вид нарушения: каждая из четырёх категорий называется своим словом ----


def test_prod_case_names_every_unexpected_field_of_start() -> None:
    """Прод-случай: четыре лишних поля в `start` — все четыре названы по имени."""
    from app.schemas.voice_frames import FRAME_START, VoiceStartFrame

    message = _message(
        VoiceStartFrame,
        FRAME_START,
        {
            "type": "start",
            "generationMode": SENTINEL,
            "userId": SENTINEL,
            "voiceId": SENTINEL,
            "session_id": SENTINEL,
        },
    )

    assert message == (
        "invalid 'start' frame: unexpected field 'generationMode'; unexpected field 'userId'; "
        "unexpected field 'voiceId'; unexpected field 'session_id'"
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # uuid_parsing — неверный формат
        (
            {"type": "start", "sessionId": SENTINEL},
            "field 'sessionId' has an invalid type or format",
        ),
        # пустая строка на месте UUID — тот же вид, что в прод-случае `"sessionId": ""`
        ({"type": "start", "sessionId": ""}, "field 'sessionId' has an invalid type or format"),
        # literal_error — недопустимое значение
        ({"type": "start", "mode": SENTINEL}, "field 'mode' has an invalid value"),
        # string_type — неверный тип
        ({"type": "start", "model": 12345}, "field 'model' has an invalid type or format"),
    ],
)
def test_start_violation_kinds(payload: dict[str, Any], expected: str) -> None:
    from app.schemas.voice_frames import FRAME_START, VoiceStartFrame

    assert _message(VoiceStartFrame, FRAME_START, payload) == f"invalid 'start' frame: {expected}"


def test_missing_required_field_is_named() -> None:
    from app.schemas.voice_frames import FRAME_INTERRUPT, VoiceInterruptFrame

    message = _message(VoiceInterruptFrame, FRAME_INTERRUPT, {"type": "interrupt"})

    assert message == (
        "invalid 'interrupt' frame: missing required field 'turnId'; "
        "missing required field 'reason'"
    )


def test_constraint_violation_is_an_invalid_value() -> None:
    """Пустой `text` (`min_length`) и пустой `results[]` — недопустимое значение, не тип."""
    from app.schemas.voice_frames import (
        FRAME_TEXT,
        FRAME_TOOL_RESULT,
        VoiceTextFrame,
        VoiceToolResultFrame,
    )

    assert _message(VoiceTextFrame, FRAME_TEXT, {"type": "text", "text": ""}) == (
        "invalid 'text' frame: field 'text' has an invalid value"
    )
    tool_result = {
        "type": "tool.result",
        "turnId": "00000000-0000-0000-0000-000000000001",
        "results": [],
    }
    assert _message(VoiceToolResultFrame, FRAME_TOOL_RESULT, tool_result) == (
        "invalid 'tool.result' frame: field 'results' has an invalid value"
    )


def test_nested_path_names_the_list_element() -> None:
    from app.schemas.voice_frames import FRAME_TOOL_RESULT, VoiceToolResultFrame

    message = _message(
        VoiceToolResultFrame,
        FRAME_TOOL_RESULT,
        {
            "type": "tool.result",
            "turnId": "00000000-0000-0000-0000-000000000001",
            "results": [{"toolCallId": SENTINEL, "extraKey": SENTINEL}],
        },
    )

    assert message == (
        "invalid 'tool.result' frame: field 'results[0].toolCallId' has an invalid type or "
        "format; unexpected field 'results[0].extraKey'"
    )


# ---- ввод клиента не отражается ----


@pytest.mark.parametrize(
    "bad_key",
    [
        f"bad key {SENTINEL}",  # не идентификатор
        "x" * 65,  # идентификатор, но длиннее 64
        "<script>",  # разметка
    ],
)
def test_unreportable_extra_key_is_named_generically(bad_key: str) -> None:
    from app.schemas.voice_frames import FRAME_START, VoiceStartFrame

    message = _message(VoiceStartFrame, FRAME_START, {"type": "start", bad_key: SENTINEL})

    assert message == "invalid 'start' frame: unexpected field"
    assert bad_key not in message


def test_unreportable_nested_extra_key_keeps_the_known_parent() -> None:
    from app.schemas.voice_frames import FRAME_TOOL_RESULT, VoiceToolResultFrame

    message = _message(
        VoiceToolResultFrame,
        FRAME_TOOL_RESULT,
        {
            "type": "tool.result",
            "turnId": "00000000-0000-0000-0000-000000000001",
            "results": [{"toolCallId": "00000000-0000-0000-0000-000000000002", "a b": 1}],
        },
    )

    assert message == "invalid 'tool.result' frame: unexpected field in 'results[0]'"


def test_identifier_of_exactly_64_chars_is_still_named() -> None:
    """Граница потолка длины: 64 символа — отражается, 65 — уже нет (кейс выше)."""
    from app.schemas.voice_frames import FRAME_START, VoiceStartFrame

    key = "k" * 64
    message = _message(VoiceStartFrame, FRAME_START, {"type": "start", key: 1})

    assert message == f"invalid 'start' frame: unexpected field '{key}'"


@pytest.mark.parametrize("index", range(6))
def test_no_frame_schema_reflects_submitted_values(index: int) -> None:
    """На КАЖДОЙ схеме кадра: значения всех полей — метка, плюс лишнее поле с меткой.

    Инвариант не зависит от вида нарушения: ни одно присланное значение не попадает в текст.
    """
    model, frame_type = _schemas()[index]
    payload: dict[str, Any] = {"type": frame_type}
    for name in model.model_fields:
        if name != "type":
            payload[name] = SENTINEL
    payload["extraField"] = SENTINEL

    message = _message(model, frame_type, payload)

    assert message.startswith(f"invalid '{frame_type}' frame: ")
    assert SENTINEL not in message
    assert "unexpected field 'extraField'" in message


# ---- потолок числа нарушений ----


def test_violation_count_is_capped_and_the_rest_is_counted() -> None:
    from app.schemas.voice_frames import FRAME_TOOL_RESULT, VoiceToolResultFrame

    message = _message(
        VoiceToolResultFrame,
        FRAME_TOOL_RESULT,
        {"type": "tool.result", "turnId": SENTINEL, "results": [3] * 20},
    )

    # 21 нарушение: `turnId` + 20 кривых элементов; названо пять, остаток — числом.
    assert message.count("field '") == 5
    assert message.endswith("; and 16 more")


# ---- неизвестный и отсутствующий `type` ----


def test_unknown_type_lists_the_allowed_types_not_the_submitted_one() -> None:
    from app.api_gateway.routers.chat_voice import _unknown_type_message
    from app.schemas.voice_frames import CLIENT_FRAME_TYPES

    message = _unknown_type_message({"type": SENTINEL})

    assert SENTINEL not in message
    assert message == "unknown frame type; expected one of: " + ", ".join(CLIENT_FRAME_TYPES)


def test_missing_type_is_named_as_a_missing_field() -> None:
    from app.api_gateway.routers.chat_voice import _unknown_type_message
    from app.schemas.voice_frames import CLIENT_FRAME_TYPES

    message = _unknown_type_message({"text": SENTINEL})

    assert SENTINEL not in message
    assert message == "missing required field 'type'; expected one of: " + ", ".join(
        CLIENT_FRAME_TYPES
    )
