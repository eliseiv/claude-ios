"""Integration: отказ `validation_error` на рабочем сокете называет, что не так с кадром.

Норма — `docs/modules/chat-orchestrator/02-api-contracts.md`, таблица отказов голосового режима,
строка «Кадр не JSON, неизвестный `type` или схема не сошлась», и `docs/API-REFERENCE.md §31`,
пункт «Кадр не по схеме». Меняется ТОЛЬКО `message`: код, `scope` и живое соединение прежние.

Прод-случай: на кадр `start` с лишними полями разработчик получал «invalid 'start' frame» и не мог
понять, что не так. Кейсы идут по КАЖДОМУ месту, где кадр клиент → сервер отвергается: шесть
схем кадров, неизвестный и отсутствующий `type`, нечитаемый JSON и JSON-не-объект.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.integration.test_voice_mode_session_adr104 import seed_voice_user
from tests.voice_harness import first_of

# Заметное значение: его появление в `message` означало бы отражение ввода клиента.
SENTINEL = "SENTINEL-7f3a-value"
_ALLOWED = "start, utterance.begin, utterance.end, text, interrupt, tool.result, ping"


async def _assert_alive(stand: Any, socket: Any) -> None:
    """Соединение живо: следующий ход проходит до `done`."""
    assert socket.close_code is None
    stand.script("Ответ после отвергнутого кадра. ")
    frames = await socket.turn()
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


# ---- `start`: отказ до открытия сеанса ----


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            # Прод-случай дословно по составу полей.
            {
                "generationMode": SENTINEL,
                "userId": SENTINEL,
                "voiceId": SENTINEL,
                "session_id": SENTINEL,
            },
            "invalid 'start' frame: unexpected field 'generationMode'; unexpected field 'userId'; "
            "unexpected field 'voiceId'; unexpected field 'session_id'",
        ),
        (
            {"sessionId": SENTINEL},
            "invalid 'start' frame: field 'sessionId' has an invalid type or format",
        ),
        ({"mode": SENTINEL}, "invalid 'start' frame: field 'mode' has an invalid value"),
    ],
)
async def test_start_rejection_names_the_field_and_the_violation(
    voice_stand: Any, fields: dict[str, Any], expected: str
) -> None:
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket = await stand.connect(uid)
    error = await socket.start(**fields)

    assert error == {
        "type": "error",
        "code": "validation_error",
        "message": expected,
        "scope": "session",
    }
    assert SENTINEL not in error["message"]
    assert socket.close_code is None
    # Сеанс не сломан: исправленный `start` принимается.
    ready = await socket.start()
    assert ready["type"] == "ready"
    await _assert_alive(stand, socket)


# ---- кадры после `start` ----


@pytest.mark.parametrize(
    ("frame", "expected", "scope"),
    [
        (
            {"type": "utterance.begin"},
            "invalid 'utterance.begin' frame: missing required field 'mediaType'",
            "session",
        ),
        (
            {"type": "utterance.begin", "mediaType": SENTINEL},
            "invalid 'utterance.begin' frame: field 'mediaType' has an invalid value",
            "session",
        ),
        (
            {"type": "utterance.end", "generationMode": SENTINEL},
            "invalid 'utterance.end' frame: field 'generationMode' has an invalid value",
            "turn",
        ),
        (
            {"type": "text", "text": "привет", "context": SENTINEL},
            "invalid 'text' frame: field 'context' has an invalid type or format",
            "turn",
        ),
        (
            {"type": "interrupt", "turnId": SENTINEL},
            "invalid 'interrupt' frame: field 'turnId' has an invalid type or format; "
            "missing required field 'reason'",
            "turn",
        ),
        (
            {
                "type": "tool.result",
                "turnId": str(uuid.uuid4()),
                "results": [{"toolCallId": SENTINEL, "extraKey": SENTINEL}],
            },
            "invalid 'tool.result' frame: field 'results[0].toolCallId' has an invalid type or "
            "format; unexpected field 'results[0].extraKey'",
            "turn",
        ),
    ],
    ids=[
        "utterance.begin-missing",
        "utterance.begin-value",
        "utterance.end",
        "text",
        "interrupt",
        "tool.result",
    ],
)
async def test_frame_rejection_names_the_field_and_the_violation(
    voice_stand: Any, frame: dict[str, Any], expected: str, scope: str
) -> None:
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket, _ = await stand.session(uid)
    await socket.send_frame(frame)
    error = await socket.next()

    assert error == {
        "type": "error",
        "code": "validation_error",
        "message": expected,
        "scope": scope,
    }
    assert SENTINEL not in error["message"]
    await _assert_alive(stand, socket)


async def test_unknown_type_lists_the_allowed_types(voice_stand: Any) -> None:
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket, _ = await stand.session(uid)
    await socket.send_frame({"type": SENTINEL})
    error = await socket.next()

    assert error == {
        "type": "error",
        "code": "validation_error",
        "message": f"unknown frame type; expected one of: {_ALLOWED}",
        "scope": "session",
    }
    assert SENTINEL not in error["message"]
    await _assert_alive(stand, socket)


async def test_missing_type_is_named(voice_stand: Any) -> None:
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket, _ = await stand.session(uid)
    await socket.send_frame({"text": SENTINEL})
    error = await socket.next()

    assert error["message"] == f"missing required field 'type'; expected one of: {_ALLOWED}"
    assert (error["code"], error["scope"]) == ("validation_error", "session")
    assert SENTINEL not in error["message"]
    await _assert_alive(stand, socket)


async def test_every_listed_type_is_actually_dispatched(voice_stand: Any) -> None:
    """Перечень в тексте отказа — это ровно те типы, которые обработчик принимает.

    Голый кадр каждого перечисленного типа (кроме `start`, уже принятого, и `ping`) доходит до
    СВОЕГО обработчика — его отвергает своя схема или своё правило, но не ветка «неизвестный
    тип»; `ping` не даёт ответа вовсе. Иначе текст отказа называл бы разработчику тип, который
    сервер тоже отвергнет как неизвестный.
    """
    from app.schemas.voice_frames import CLIENT_FRAME_TYPES, FRAME_PING, FRAME_START

    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    socket, _ = await stand.session(uid)

    for frame_type in CLIENT_FRAME_TYPES:
        if frame_type in (FRAME_START, FRAME_PING):
            continue
        await socket.send_frame({"type": frame_type})
        error = await socket.next()
        assert error["type"] == "error", error
        assert "expected one of" not in error["message"], (frame_type, error)
        assert f"'{frame_type}'" in error["message"], (frame_type, error)

    await socket.send_frame({"type": FRAME_PING})
    await socket.send_frame({"type": "utterance.begin"})
    # Первый кадр после `ping` — ответ на СЛЕДУЮЩИЙ кадр: на сам `ping` сервер не ответил.
    after_ping = await socket.next()
    assert "'utterance.begin'" in after_ping["message"], after_ping


# ---- кадр, который не разобрать ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"{{not json {SENTINEL}", "frame is not valid JSON"),
        (f'["{SENTINEL}"]', "frame must be a JSON object"),
        (f'"{SENTINEL}"', "frame must be a JSON object"),
    ],
    ids=["not-json", "json-array", "json-string"],
)
async def test_unparsable_frame_names_the_violation(
    voice_stand: Any, raw: str, expected: str
) -> None:
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket, _ = await stand.session(uid)
    await socket._to_app.put({"type": "websocket.receive", "text": raw})  # noqa: SLF001
    error = await socket.next()

    assert error == {
        "type": "error",
        "code": "validation_error",
        "message": expected,
        "scope": "session",
    }
    await _assert_alive(stand, socket)
