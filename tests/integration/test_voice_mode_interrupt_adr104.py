"""Integration: прерывание голосового хода (ADR-104 §5).

Норма — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`, раздел
«Integration — прерывание (оба края предиката, отдельными тестами)».

Момент прерывания задаётся ПРИЧИННО, а не паузой: дельта, следующая за `interrupt`, не выходит,
пока кадр не обработан обработчиком сокета (`interrupt_barrier`). Пауза здесь была бы гонкой.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.observability.metrics import voice_mode_turns_total
from tests.integration.test_voice_mode_session_adr104 import (
    balance_of,
    chat_steps,
    ledger_rows,
    seed_voice_user,
)
from tests.voice_harness import (
    FIVE_SENTENCES,
    delta_text,
    first_of,
    frames_of,
    interrupt_barrier,
)


def _payload(step: dict[str, Any]) -> dict[str, Any]:
    raw = step["payload"]
    return raw if isinstance(raw, dict) else json.loads(raw)


def _assistant_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in steps if step["role"] == "assistant"]


async def _read_until_first_audio_end(socket: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        frame = await socket.next()
        frames.append(frame)
        if frame["type"] == "audio.end":
            return frames


# ---------------------------------------------------------------------------------------------
# Край предиката: текст накоплен
# ---------------------------------------------------------------------------------------------


async def test_interrupt_with_text_saves_the_prefix_and_bills_the_turn(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Текст накоплен → префикс сохранён, ход списан ПОЛНОСТЬЮ (diff).

    Падает на реализации, бросающей прерванный ход без шага ассистента: там реплика осталась бы
    без ответа и следующий ход отвечал бы на неё (прод `avelyra` 2026-09-09). Падает и на
    реализации «прерванное бесплатно»: перебивание стало бы бесплатной генерацией.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until_first_audio_end(socket)
    turn_id = first_of(head, "transcript")["turnId"]

    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "barge_in"})
    tail = await socket.collect_until("done")
    frames = head + tail

    interrupted = first_of(frames, "interrupted")
    assert interrupted["reason"] == "barge_in"
    assert isinstance(interrupted["spokenSegments"], int)
    assert interrupted["spokenSegments"] >= 1
    # Приходит НЕПОСРЕДСТВЕННО перед `done`.
    control = [f["type"] for f in frames if f["type"] not in ("__bytes__", "__close__")]
    assert control[-2:] == ["interrupted", "done"]

    steps = await chat_steps(stand, ready["sessionId"])
    assistant = _assistant_steps(steps)
    assert len(assistant) == 1, "прерванный ход обязан оставить шаг ассистента"
    payload = _payload(assistant[0])
    assert payload["interrupted"]["reason"] == "barge_in"
    assert payload["interrupted"]["spokenSegments"] == interrupted["spokenSegments"]
    assert "turnFailed" not in payload
    # Шаг содержит РОВНО накопленный префикс — то, что клиент получил кадрами `delta`.
    assert delta_text(frames) in json.dumps(payload, ensure_ascii=False)

    ledger = await ledger_rows(stand, uid)
    turn_rows = [row for row in ledger if row["idempotency_key"] == turn_id]
    assert len(turn_rows) == 1, "ровно одно списание хода по messageStepId"
    assert await balance_of(stand, uid) == 100 - sum(abs(row["amount"]) for row in ledger)


async def test_spoken_segments_is_a_number_of_heard_segments(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`spokenSegments` — ЧИСЛО дослушанных сегментов, а не массив текстов.

    Падает на реализации, кладущей туда произнесённый текст: это была бы вторая копия
    пользовательского контента и второй источник одной величины.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], FIVE_SENTENCES[1], barrier, *FIVE_SENTENCES[2:])

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until_first_audio_end(socket)
    turn_id = first_of(head, "transcript")["turnId"]
    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "user_stop"})
    tail = await socket.collect_until("done")

    delivered = len(frames_of(head + tail, "audio.end"))
    assert first_of(tail, "interrupted")["spokenSegments"] == delivered

    steps = await chat_steps(stand, ready["sessionId"])
    payload = _payload(_assistant_steps(steps)[0])
    assert payload["interrupted"]["spokenSegments"] == delivered
    assert isinstance(payload["interrupted"]["spokenSegments"], int)


# ---------------------------------------------------------------------------------------------
# Край предиката: текст не накоплен
# ---------------------------------------------------------------------------------------------


async def test_interrupt_before_the_first_delta_does_not_cancel_generation(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Текст НЕ накоплен → генерация не отменяется (diff), 09-testing §Прерывание, строка 2.

    Нормативный предикат — «успел ли ассистент произвести хоть один символ текста» на момент
    прерывания (`ADR-104 §5`, таблица, строка «пусто»). Кадр `interrupt` приходит ДО первой
    дельты, поэтому ход обязан дойти до штатного конца: `audio.*` больше не приходят, `done`
    несёт ПОЛНЫЙ ответ, шаг ассистента персистится БЕЗ пометки, ход списан.

    Тест падает на реализации, отменяющей генерацию всегда: там ход остался бы без шага
    ассистента либо получил бы пометку `interrupted` на пустом накопителе.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(barrier, *FIVE_SENTENCES)

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    transcript = await socket.next()
    assert transcript["type"] == "transcript"
    await socket.send_frame(
        {"type": "interrupt", "turnId": transcript["turnId"], "reason": "barge_in"}
    )
    frames = await socket.collect_until("done")

    # Звука по этому ходу больше не будет — это ровно то, о чём просил пользователь.
    assert frames_of(frames, "audio.end") == []
    # …но ответ доходит целиком.
    answer = first_of(frames, "done")["response"]["assistantMessage"]
    for sentence in FIVE_SENTENCES:
        assert sentence.strip() in answer
    # `interrupted` всё равно приходит перед `done`.
    assert frames_of(frames, "interrupted")

    steps = await chat_steps(stand, ready["sessionId"])
    assistant = _assistant_steps(steps)
    assert len(assistant) == 1
    payload = _payload(assistant[0])
    assert "interrupted" not in payload, "на пустом накопителе пометки быть не должно"
    assert "turnFailed" not in payload

    ledger = await ledger_rows(stand, uid)
    assert [row["idempotency_key"] for row in ledger].count(transcript["turnId"]) == 1


# ---------------------------------------------------------------------------------------------
# Несущий инвариант и разделение пометок
# ---------------------------------------------------------------------------------------------


async def test_next_turn_after_an_interrupt_answers_the_new_utterance(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прерванный ход не оставляет реплику без ответа, а следующий отвечает на НОВУЮ реплику.

    Заодно: прерывание не выключает синтез навсегда — следующий ход звучит как обычно.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    barrier = interrupt_barrier(monkeypatch)
    stand.transcription.transcripts.extend(["первая реплика", "вторая реплика"])
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    stand.script("Ответ на вторую реплику. ")

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until_first_audio_end(socket)
    turn_id = first_of(head, "transcript")["turnId"]
    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "barge_in"})
    await socket.collect_until("done")

    second = await socket.turn()

    # У прерванного хода есть шаг ассистента — иначе модель отвечала бы на прошлую реплику.
    steps = await chat_steps(stand, ready["sessionId"])
    by_turn = {str(step["message_step_id"]) for step in _assistant_steps(steps)}
    assert turn_id in by_turn

    # Последний вызов провайдера получил ВТОРУЮ реплику, а не первую.
    last_call = stand.llm.calls[-1]
    wire = json.dumps(last_call["messages"], ensure_ascii=False, default=str)
    assert "вторая реплика" in wire
    assert wire.rindex("вторая реплика") > wire.rindex("первая реплика")

    # Синтез не выключен навсегда.
    assert frames_of(second, "audio.end"), "следующий ход обязан звучать как обычно"


async def test_interrupted_and_turn_failed_are_different_marks(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`interrupted` ≠ `turnFailed` (diff, обе стороны). Падает при схлопывании пометок."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until_first_audio_end(socket)
    turn_id = first_of(head, "transcript")["turnId"]
    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "barge_in"})
    await socket.collect_until("done")

    interrupted_payload = _payload(_assistant_steps(await chat_steps(stand, ready["sessionId"]))[0])
    assert "interrupted" in interrupted_payload
    assert "turnFailed" not in interrupted_payload

    # Обратная сторона: ход, упавший на отказе провайдера, несёт `turnFailed` и НЕ `interrupted`.
    stand.llm.raise_upstream = True
    await socket.begin_utterance(audio=b"second-utterance")
    failed_frames = await socket.collect_until("error")
    error = first_of(failed_frames, "error")
    assert (error["code"], error["scope"]) == ("upstream_error", "turn")

    steps = await chat_steps(stand, ready["sessionId"])
    failed_steps = [
        step for step in _assistant_steps(steps) if str(step["message_step_id"]) != turn_id
    ]
    assert failed_steps, "упавший ход обязан оставить шаг с пометкой отказа"
    failed_payload = _payload(failed_steps[0])
    assert "turnFailed" in failed_payload
    assert "interrupted" not in failed_payload


async def test_an_ordinary_turn_carries_neither_mark(voice_stand: Any) -> None:
    """Против переоценки: обычный, не прерванный ход не несёт НИ ОДНОЙ из двух пометок."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script(*FIVE_SENTENCES)

    socket, ready = await stand.session(uid)
    frames = await socket.turn()

    assert frames_of(frames, "interrupted") == []
    steps = await chat_steps(stand, ready["sessionId"])
    payload = _payload(_assistant_steps(steps)[0])
    assert "interrupted" not in payload
    assert "turnFailed" not in payload
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


# ---------------------------------------------------------------------------------------------
# §13.11 — снимок в момент кадра и величина ХОДА
# ---------------------------------------------------------------------------------------------


async def test_interrupted_frame_arrives_in_both_rows_of_the_predicate(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кадр `interrupted` приходит в ОБЕИХ строках — он подтверждает НАМЕРЕНИЕ, а не отмену.

    Против чтения «`interrupted` = отмена»: в строке «пусто» генерация не отменялась, ответ дошёл
    целиком и пометки в шаге нет, а кадр всё равно пришёл непосредственно перед `done`, и исход
    метрики хода — `interrupted`. Падает на реализации, привязавшей кадр к факту отмены.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    # Дельты идут ПОСЛЕ кадра — без этого кейс зелен и на чтении накопителя при дельте.
    stand.script(barrier, *FIVE_SENTENCES)
    before = voice_mode_turns_total.labels(outcome="interrupted")._value.get()  # noqa: SLF001

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    transcript = await socket.next()
    await socket.send_frame(
        {"type": "interrupt", "turnId": transcript["turnId"], "reason": "user_stop"}
    )
    frames = await socket.collect_until("done")

    control = [f["type"] for f in frames if f["type"] not in ("__bytes__", "__close__")]
    assert control[-2:] == ["interrupted", "done"], "кадр приходит непосредственно перед `done`"
    assert voice_mode_turns_total.labels(outcome="interrupted")._value.get() == (  # noqa: SLF001
        before + 1
    )
    # …и при этом генерация НЕ отменялась: ответ целиком, пометки в шаге нет.
    answer = first_of(frames, "done")["response"]["assistantMessage"]
    for sentence in FIVE_SENTENCES:
        assert sentence.strip() in answer
    payload = _payload(_assistant_steps(await chat_steps(stand, ready["sessionId"]))[0])
    assert "interrupted" not in payload


async def test_predicate_snapshot_is_a_turn_value_not_a_leg_value(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Предикат считается по ХОДУ, а не по ноге (diff, §13.11).

    Нога `tool_call` уже отдала сопутствующий текст и звук — пользователь его УСЛЫШАЛ; `interrupt`
    приходит на ноге `continuation` ДО её первой дельты. Ход обязан отмениться на первой же дельте
    continuation, а шаг — нести `payload.interrupted`.

    Падает на реализации, снимающей предикат с ПОНОЖНОГО накопителя: он на continuation обнулён,
    предикат дал бы «пусто», и ассистент договорил бы целый новый ответ после того, как его
    попросили замолчать.
    """
    tool_call = ("calendar.read", {"start": "2026-09-10T00:00:00Z", "end": "2026-09-11T00:00:00Z"})
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script_tool_call([tool_call], "Сейчас посмотрю календарь и сразу отвечу. ")

    socket, ready = await stand.session(uid)
    first_leg = await socket.turn()
    tool_done = first_of(first_leg, "done")["response"]
    assert tool_done["status"] == "tool_call"
    assert frames_of(first_leg, "audio.end"), "сопутствующий текст ноги tool_call УСЛЫШАН"

    barrier = interrupt_barrier(monkeypatch)
    stand.script(barrier, *FIVE_SENTENCES)
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": tool_done["messageStepId"],
            "results": [{"toolCallId": tool_done["toolCalls"][0]["id"], "result": {"ok": True}}],
        }
    )
    await socket.send_frame(
        {"type": "interrupt", "turnId": tool_done["messageStepId"], "reason": "barge_in"}
    )
    second_leg = await socket.collect_until("done")

    answer = first_of(second_leg, "done")["response"]["assistantMessage"] or ""
    assert FIVE_SENTENCES[-1].strip() not in answer, "нога continuation обязана отмениться"
    payloads = [
        _payload(step)
        for step in _assistant_steps(await chat_steps(stand, ready["sessionId"]))
        if str(step["message_step_id"]) == tool_done["messageStepId"]
    ]
    assert any("interrupted" in p for p in payloads)
