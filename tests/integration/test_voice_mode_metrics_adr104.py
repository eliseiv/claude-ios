"""Integration: наблюдаемость голосового режима (ADR-104 §12).

Норма — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`, раздел «Метрики».

Каждый исход проверяется ДВАЖДЫ: инкремент серии и НАБЛЮДАЕМЫЕ ФАКТЫ пути, из которых предикат
отнесения вычисляется (наличие шага ассистента, наличие и величина строки леджера, наличие
пометки, был ли вызван синтезатор, отправлен ли `audio.end`, значение `truncated`). Совпадение
лейбла с ожиданием, скопированным из спеки, доказывало бы лишь непротиворечивость спеки самой
себе.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.observability.metrics import (
    voice_mode_connections,
    voice_mode_speech_segments_total,
    voice_mode_turns_total,
)
from tests.conftest import auth_headers
from tests.integration.test_voice_mode_billing_adr104 import tts_keys
from tests.integration.test_voice_mode_session_adr104 import (
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
    upstream_failure,
)

_TURN_OUTCOMES = ("ok", "interrupted", "blocked", "upstream_error", "disconnected")
_SEGMENT_OUTCOMES = (
    "ok",
    "capped",
    "skipped_empty",
    "interrupted",
    "upstream_error",
    "rate_limited",
)


def _turns() -> dict[str, float]:
    return {
        outcome: voice_mode_turns_total.labels(outcome=outcome)._value.get()  # noqa: SLF001
        for outcome in _TURN_OUTCOMES
    }


def _segments() -> dict[str, float]:
    return {
        outcome: voice_mode_speech_segments_total.labels(outcome=outcome)._value.get()  # noqa: SLF001
        for outcome in _SEGMENT_OUTCOMES
    }


def _delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {key: after[key] - before[key] for key in before}


def _connections() -> float:
    return voice_mode_connections._value.get()  # noqa: SLF001


def _payload(step: dict[str, Any]) -> dict[str, Any]:
    raw = step["payload"]
    return raw if isinstance(raw, dict) else json.loads(raw)


async def _read_until_first_audio_end(socket: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while not frames or frames[-1]["type"] != "audio.end":
        frames.append(await socket.next())
    return frames


# ---------------------------------------------------------------------------------------------
# `voice_mode_turns_total` — пять исходов, пять кейсов
# ---------------------------------------------------------------------------------------------


async def test_ok_turn_increments_ok_and_nothing_else(voice_stand: Any) -> None:
    """Штатный ход → `outcome="ok"` +1. Наблюдаемые факты: шаг есть, списание есть, пометок нет."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)
    before = _turns()

    socket, ready = await stand.session(uid)
    frames = await socket.turn()

    assert _delta(before, _turns()) == {"ok": 1, **{k: 0 for k in _TURN_OUTCOMES if k != "ok"}}
    done = first_of(frames, "done")["response"]
    steps = await chat_steps(stand, ready["sessionId"])
    assistant = [step for step in steps if step["role"] == "assistant"]
    assert len(assistant) == 1
    payload = _payload(assistant[0])
    assert "interrupted" not in payload and "turnFailed" not in payload
    assert done["messageStepId"] in [
        row["idempotency_key"] for row in await ledger_rows(stand, uid)
    ]


async def test_interrupted_turn_increments_interrupted_only(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прерванный ход → `interrupted` +1 и НИ ОДНОГО инкремента аварийных классов.

    Против переоценки в чистом виде: `interrupted` — самый частый штатный исход голосового
    режима, и отнесение его к тревожным обесценило бы всю серию.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    before = _turns()

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until_first_audio_end(socket)
    turn_id = first_of(head, "transcript")["turnId"]
    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "barge_in"})
    await socket.collect_until("done")

    delta = _delta(before, _turns())
    assert delta["interrupted"] == 1
    assert delta["upstream_error"] == 0
    assert delta["disconnected"] == 0
    assert delta["ok"] == 0
    # Наблюдаемые факты пути: шаг ассистента ЕСТЬ, пометка `interrupted` есть, `turnFailed` нет.
    payload = _payload(
        [s for s in await chat_steps(stand, ready["sessionId"]) if s["role"] == "assistant"][0]
    )
    assert "interrupted" in payload and "turnFailed" not in payload


async def test_blocked_turn_increments_blocked(voice_stand: Any) -> None:
    """Policy-блок → `blocked` +1. Наблюдаемый факт: `status="blocked"`, синтезатор не вызван."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=0)
    stand.script(*FIVE_SENTENCES)
    before = _turns()

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    delta = _delta(before, _turns())
    assert delta["blocked"] == 1
    assert delta["upstream_error"] == 0
    assert first_of(frames, "done")["response"]["status"] == "blocked"
    assert stand.speech.calls == 0


async def test_provider_failure_increments_upstream_error(voice_stand: Any) -> None:
    """Отказ провайдера → `upstream_error` +1. Наблюдаемые факты: пометка есть, списания нет."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(FIVE_SENTENCES[0], upstream_failure())
    before = _turns()

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    frames = await socket.collect_until("error")

    delta = _delta(before, _turns())
    assert delta["upstream_error"] == 1
    assert delta["disconnected"] == 0, "поломка поставщика с мобильной сетью не сливается"
    payload = _payload(
        [s for s in await chat_steps(stand, ready["sessionId"]) if s["role"] == "assistant"][-1]
    )
    assert "turnFailed" in payload
    turn_id = first_of(frames, "transcript")["turnId"]
    assert turn_id not in [row["idempotency_key"] for row in await ledger_rows(stand, uid)]


async def test_socket_closed_before_done_increments_disconnected(voice_stand: Any) -> None:
    """Закрытие сокета до `done` → `disconnected` +1, а НЕ `upstream_error`.

    Наблюдение, а не авария: шаг персистится и ход тарифицируется — ушедший клиент не отменяет
    хода и не означает, что провайдер отказал.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)
    before = _turns()

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    await socket.disconnect()
    await socket.aclose()

    delta = _delta(before, _turns())
    assert delta["disconnected"] == 1
    assert delta["upstream_error"] == 0
    assert delta["ok"] == 0
    steps = await chat_steps(stand, ready["sessionId"])
    assistant = [step for step in steps if step["role"] == "assistant"]
    assert len(assistant) == 1
    assert "turnFailed" not in _payload(assistant[0])
    assert [row["idempotency_key"] for row in await ledger_rows(stand, uid)]


# ---------------------------------------------------------------------------------------------
# `voice_mode_connections`
# ---------------------------------------------------------------------------------------------


async def test_connections_gauge_rises_on_accept_and_falls_on_close(voice_stand: Any) -> None:
    """Серия открытых соединений растёт на `accept` и падает на закрытии."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    before = _connections()

    socket = await stand.connect(uid)
    assert _connections() == before + 1

    await socket.disconnect()
    await socket.aclose()
    assert _connections() == before


async def test_connections_gauge_falls_on_the_idle_timeout(voice_stand: Any) -> None:
    """…в том числе на закрытии по idle-таймауту: close `4408`, серия возвращается к прежнему.

    Молчание здесь — предмет кейса, а не способ синхронизации: таймаут измеряется по часам.
    """
    stand = await voice_stand(VOICE_MODE_IDLE_TIMEOUT_SECONDS="0.05")
    uid = await seed_voice_user(stand)
    before = _connections()

    socket = await stand.connect(uid)
    closed = await socket.next()

    assert closed["type"] == "__close__"
    assert closed["code"] == 4408
    await socket.aclose()
    assert _connections() == before


# ---------------------------------------------------------------------------------------------
# `voice_mode_speech_segments_total` — пять значений, пять кейсов
# ---------------------------------------------------------------------------------------------


async def test_delivered_segment_is_ok(voice_stand: Any) -> None:
    """Сегмент доставлен целиком → `ok`. Наблюдаемый факт: `audio.end` с `truncated: false`."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)
    before = _segments()

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    ends = frames_of(frames, "audio.end")
    assert len(ends) == len(FIVE_SENTENCES)
    assert all(end["truncated"] is False for end in ends)
    delta = _delta(before, _segments())
    assert delta["ok"] == len(FIVE_SENTENCES)
    assert delta["capped"] == 0
    assert delta["upstream_error"] == 0


async def test_capped_segment_is_capped_and_not_an_incident(voice_stand: Any) -> None:
    """Сегмент, на котором сработал совокупный потолок, → `capped`, и это НЕ авария.

    Против переоценки: `capped` — штатный исход длинного ответа, ровно то, ради чего потолок и
    существует. Наблюдаемый факт — `audio.end` именно этого сегмента несёт `truncated: true`.
    """
    stand = await voice_stand(TTS_MAX_CHARS="80")
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)
    before = _segments()

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    ends = frames_of(frames, "audio.end")
    assert ends[-1]["truncated"] is True
    assert all(end["truncated"] is False for end in ends[:-1])
    delta = _delta(before, _segments())
    assert delta["capped"] == 1, "один потолок считается один раз, а не по длине хвоста"
    assert delta["upstream_error"] == 0
    assert delta["interrupted"] == 0


async def test_empty_after_cleaning_segment_is_skipped_empty(voice_stand: Any) -> None:
    """Кандидат, пустой после чистки, → `skipped_empty`, и синтезатор НЕ вызван.

    Против недооценки: это НЕ `upstream_error` — поставщика здесь не звали вовсе.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script("```python\nprint(1)\nprint(2)\n```")
    before = _segments()

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    assert stand.speech.calls == 0
    assert frames_of(frames, "audio.end") == []
    delta = _delta(before, _segments())
    assert delta["skipped_empty"] == 1
    assert delta["upstream_error"] == 0
    assert delta["ok"] == 0


async def test_segment_interrupted_mid_synthesis_is_interrupted(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`interrupt` посреди синтеза сегмента → `interrupted`, и `audio.end` этого сегмента НЕТ.

    Момент задан причинно: второй вызов синтезатора удерживается до тех пор, пока кадр
    `interrupt` не обработан обработчиком сокета.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.speech.hold_on = 2
    stand.speech.hold = barrier
    # Генерация тоже ждёт кадра `interrupt`: без этого ход закрылся бы раньше удержанного сегмента.
    stand.script(FIVE_SENTENCES[0], FIVE_SENTENCES[1], barrier)
    before = _segments()

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until_first_audio_end(socket)
    turn_id = first_of(head, "transcript")["turnId"]
    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "barge_in"})
    tail = await socket.collect_until("done")

    ends = frames_of(head + tail, "audio.end")
    assert [end["segment"] for end in ends] == [0], "у удержанного сегмента `audio.end` не уходит"
    assert stand.speech.calls == 2, "второй сегмент синтезировался, но доставлен не был"
    delta = _delta(before, _segments())
    assert delta["interrupted"] == 1
    assert delta["upstream_error"] == 0
    assert delta["skipped_empty"] == 0


async def test_synthesizer_failure_is_upstream_error_once(voice_stand: Any) -> None:
    """Фейк синтеза бросает → `upstream_error` РОВНО один раз, хвост меток не получает.

    Против недооценки: отказ поставщика не маскируется под `skipped_empty`. Против переоценки:
    одна авария не умножается на длину ответа — иначе алерт обесценился бы.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.speech.fail_on = 2
    stand.script(*FIVE_SENTENCES)
    before = _segments()

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    speech_errors = [f for f in frames_of(frames, "error") if f["scope"] == "speech"]
    assert len(speech_errors) == 1
    delta = _delta(before, _segments())
    assert delta["upstream_error"] == 1
    assert delta["skipped_empty"] == 0
    assert delta["capped"] == 0
    assert delta["ok"] == 1, "первый сегмент был доставлен и остаётся `ok`"
    assert tts_keys(await ledger_rows(stand, uid)), "доставленный сегмент оплачен"
    assert first_of(frames, "done")["response"]["assistantMessage"] == delta_text(frames)


# ---------------------------------------------------------------------------------------------
# 13.8 Точное исчерпание бюджета — `speechTruncated` и второй способ `capped`
# ---------------------------------------------------------------------------------------------

_EXACT = [
    "Ровно сорок символов занимает эта фраза. ",
    "Ещё одно предложение того же ответа. ",
]


async def test_exact_budget_exhaustion_is_observable_on_the_turn(voice_stand: Any) -> None:
    """Несущий кейс §13.8: бюджет исчерпан РОВНО, и признак переносится на ход.

    `audio.end` такого сегмента честно несёт `truncated: false` (лгать на сегменте запрещено: за
    ним могло ничего и не следовать), дальнейших `audio.*` нет, а `done` несёт
    `speechTruncated: true`. Падает и на реализации, где признак остался только на сегменте (там
    `speechTruncated` не было бы вовсе), и на реализации, помечающей такой сегмент
    `truncated: true`.
    """
    exact = len(_EXACT[0].strip())
    stand = await voice_stand(TTS_MAX_CHARS=str(exact))
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*_EXACT)
    before = _segments()

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    ends = frames_of(frames, "audio.end")
    assert len(ends) == 1, "прозвучал ровно один сегмент — на нём бюджет кончился"
    assert ends[0]["truncated"] is False, "сегмент отдан целиком, и лгать о нём нельзя"
    done = first_of(frames, "done")
    assert done["speechTruncated"] is True
    assert done["response"]["assistantMessage"] == delta_text(frames)
    # Второй способ исчерпания тоже считается, и ровно один раз на ход.
    assert _delta(before, _segments())["capped"] == 1


async def test_speech_truncated_is_false_when_everything_was_spoken(voice_stand: Any) -> None:
    """Против переоценки: ответ уложился в бюджет → `speechTruncated: false`.

    Падает, если признак выставляется всякий раз, когда бюджет израсходован «под ноль» без
    остатка текста.
    """
    single = ["Единственное предложение ответа. "]
    stand = await voice_stand(TTS_MAX_CHARS=str(len(single[0].strip())))
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*single)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    ends = frames_of(frames, "audio.end")
    assert ends and all(end["truncated"] is False for end in ends)
    assert first_of(frames, "done")["speechTruncated"] is False


async def test_ordinary_truncation_agrees_on_both_levels(voice_stand: Any) -> None:
    """Обычная обрезка: `audio.end.truncated: true` И `done.speechTruncated: true`.

    Две величины согласованы там, где обе наблюдаемы, и расходятся только при ТОЧНОМ исчерпании.
    """
    stand = await voice_stand(TTS_MAX_CHARS="80")
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    assert frames_of(frames, "audio.end")[-1]["truncated"] is True
    assert first_of(frames, "done")["speechTruncated"] is True


async def test_speech_truncated_lives_on_the_frame_not_in_the_response(voice_stand: Any) -> None:
    """`speechTruncated` живёт на КАДРЕ (регрессия против расползания поля).

    Множество ключей `done.response` по-прежнему совпадает с эталоном `POST /v1/chat/v2/run`, а
    признак стоит РЯДОМ с `response`. Падает, если поле положили в `ChatResponse`.
    """
    stand = await voice_stand(TTS_MAX_CHARS="80")
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    done = first_of(await socket.turn(), "done")
    assert "speechTruncated" in done
    assert "speechTruncated" not in done["response"]

    stand.script(*FIVE_SENTENCES)
    http = await stand.http.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "то же самое", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert http.status_code == 200, http.text
    assert set(done["response"]) == set(http.json())


async def test_rate_limited_segment_outcome_is_its_own_value(voice_stand: Any) -> None:
    """Шаг, которому бакет не выдал токен, → `rate_limited`, ровно ОДИН инкремент на шаг.

    Наблюдаемые факты: токен не выдан, синтезатор не вызван, `audio.end` не отправлен.
    Против недооценки: это не `skipped_empty` (текст есть) и не `upstream_error` (поставщика не
    звали). Против переоценки: сработавшая защита в аварийные классы не попадает.
    """
    stand = await voice_stand(TTS_RATE_LIMIT_PER_MIN="1")
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    await socket.turn()
    calls_after_first = stand.speech.calls
    before = _segments()

    second = await socket.turn()

    assert stand.speech.calls == calls_after_first, "синтезатор не вызван"
    assert frames_of(second, "audio.end") == []
    delta = _delta(before, _segments())
    assert delta["rate_limited"] == 1, "ровно один инкремент на погашенный шаг"
    assert delta["skipped_empty"] == 0
    assert delta["upstream_error"] == 0
    assert delta["capped"] == 0
