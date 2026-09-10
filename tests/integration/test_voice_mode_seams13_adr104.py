"""Integration: швы между частями голосового режима (ADR-104 §13.12–§13.16).

Состав кейсов — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`. Все пять норм
закрывают СТЫКИ, а не части: окно пост-обработки между результатом оркестратора и кадром `done`,
очередь реплики после прерывания, межпроцессная последовательность по сессии, единица серии
ходов и цена рукопожатия в бакете ходов.

⚠️ Пороги кейсы задают САМИ (врезка того же раздела). Замок сессии берётся ПО-НАСТОЯЩЕМУ: стенд
подменяет Redis дублёром в памяти, потому что на живом отказе Redis обработчик уходит в
fail-open и кейс о замке не проверял бы ничего.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from app.instance_config.settings_registry import SETTING_CHAT_VOICE_INPUT_ENABLED
from app.observability.metrics import voice_mode_turns_total
from tests.conftest import auth_headers
from tests.integration.test_voice_mode_billing_adr104 import tts_keys
from tests.integration.test_voice_mode_norms13_adr104 import _voice_input_overlay
from tests.integration.test_voice_mode_session_adr104 import (
    balance_of,
    chat_steps,
    ledger_rows,
    seed_voice_user,
)
from tests.voice_harness import (
    FIVE_SENTENCES,
    VoiceHandshakeDenied,
    delta_text,
    first_of,
    frames_of,
    interrupt_barrier,
    upstream_failure,
)

_TOOL_CALL = ("calendar.read", {"start": "2026-09-10T00:00:00Z", "end": "2026-09-11T00:00:00Z"})
_TURN_OUTCOMES = ("ok", "interrupted", "blocked", "upstream_error", "disconnected")


def _payload(step: dict[str, Any]) -> dict[str, Any]:
    raw = step["payload"]
    return raw if isinstance(raw, dict) else json.loads(raw)


def _turns() -> dict[str, float]:
    return {
        outcome: voice_mode_turns_total.labels(outcome=outcome)._value.get()  # noqa: SLF001
        for outcome in _TURN_OUTCOMES
    }


def _delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {key: after[key] - before[key] for key in before}


async def _read_until(socket: Any, frame_type: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while not frames or frames[-1]["type"] != frame_type:
        frames.append(await socket.next())
    return frames


async def _tool_leg(stand: Any, uid: Any, *deltas: Any) -> tuple[Any, dict[str, Any], str]:
    stand.script_tool_call([_TOOL_CALL], *deltas)
    socket, ready = await stand.session(uid)
    done = first_of(await socket.turn(), "done")["response"]
    assert done["status"] == "tool_call"
    return socket, done, ready["sessionId"]


async def _send_tool_result(socket: Any, done: dict[str, Any]) -> None:
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": done["messageStepId"],
            "results": [{"toolCallId": done["toolCalls"][0]["id"], "result": {"ok": True}}],
        }
    )


# ---------------------------------------------------------------------------------------------
# 13.12 `done` уходит при ЛЮБОМ исходе пост-обработки состоявшегося хода
# ---------------------------------------------------------------------------------------------


async def test_failed_speech_debit_does_not_suppress_done(voice_stand: Any) -> None:
    """Несущий кейс §13.12: баланс РОВНО в цену хода, три проверки в одном прогоне.

    Балансовый гейт синтеза снимается ДО хода, а собственное списание хода уходит ВНУТРИ него,
    поэтому баланс, равный цене хода, гейт проходит, ходом обнуляется и роняет списание синтеза
    — путь детерминированный, а не экзотический.

    (а) `done` приходит и несёт полный `ChatResponse`; (б) строки `tts:` нет ни одной, баланс 0 —
    убыток наш и молчаливый; (в) наружу не приходит ни `insufficient_credits`, ни
    `speech.skipped`: звук уже прозвучал, и оба были бы ложью о состоявшемся.
    """
    stand = await voice_stand(TTS_CREDIT_COST="1")
    uid = await seed_voice_user(stand, balance=1)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    assert frames_of(frames, "audio.end"), "ход обязан быть озвучен хотя бы одним сегментом"
    done = first_of(frames, "done")["response"]
    assert done["status"] == "assistant_message"
    assert done["usage"] is not None
    assert done["assistantMessage"] == delta_text(frames)

    ledger = await ledger_rows(stand, uid)
    assert tts_keys(ledger) == [], "синтез не оплачен — это названный остаточный риск §6"
    assert done["messageStepId"] in [row["idempotency_key"] for row in ledger], "ход списан"
    assert await balance_of(stand, uid) == 0

    assert frames_of(frames, "speech.skipped") == [], "звук прозвучал — `skipped` был бы ложью"
    assert [f for f in frames_of(frames, "error") if f["code"] == "insufficient_credits"] == []


async def test_failed_speech_debit_keeps_the_outcome_ok(voice_stand: Any) -> None:
    """Против переоценки: бизнес-исход «кончились деньги» не отдаётся алерту об аварии.

    `upstream_error` — авария, на которой §12 строит алерт; шум внутри класса приучает
    игнорировать класс.
    """
    stand = await voice_stand(TTS_CREDIT_COST="1")
    uid = await seed_voice_user(stand, balance=1)
    stand.script(*FIVE_SENTENCES)
    before = _turns()

    socket, _ = await stand.session(uid)
    await socket.turn()

    delta = _delta(before, _turns())
    assert delta["ok"] == 1
    assert delta["upstream_error"] == 0
    assert delta["blocked"] == 0


async def test_failed_speech_debit_does_not_lose_the_auto_title(
    voice_stand: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Автозаголовок переживает отказ списания (diff): транзакция сокета не уносится с ним.

    Заодно фиксируется наблюдаемость убытка: WARNING `voice_speech_debit_skipped`.
    """
    from app.chat.repository import derive_title

    stand = await voice_stand(TTS_CREDIT_COST="1")
    uid = await seed_voice_user(stand, balance=1)
    stand.transcription.transcripts.append("реплика с исчерпанным балансом")
    stand.script(*FIVE_SENTENCES)

    socket, ready = await stand.session(uid)
    # Захват WARNING делается НЕ через корневой логгер: в полном прогоне до него не доходит
    # ничего. Два независимых производителя ломают корень — миграция в процессе зовёт
    # `fileConfig(disable_existing_loggers=True)` и ГАСИТ уже созданные логгеры `app.*` (тот же
    # обход стоит в четырёх соседних кейсах этого репозитория), а `configure_logging` в lifespan
    # приложения делает `root.handlers.clear()` и уносит хендлер pytest. Поэтому хендлер
    # вешается прямо на логгер-эмитент, а его флаг `disabled` снимается явно: так кейс не
    # зависит ни от одного из двух.
    target = logging.getLogger("app.api_gateway.routers.chat_voice")
    was_disabled, was_level = target.disabled, target.level
    target.disabled = False
    target.setLevel(logging.WARNING)
    target.addHandler(caplog.handler)
    try:
        await socket.turn()
    finally:
        target.removeHandler(caplog.handler)
        target.disabled, target.level = was_disabled, was_level

    listing = await stand.http.get("/v1/chats", headers=auth_headers(uid))
    row = next(r for r in listing.json()["items"] if r["id"] == ready["sessionId"])
    assert row["title"] == derive_title("реплика с исчерпанным балансом")
    assert any("voice_speech_debit_skipped" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------------------------
# 13.13 Реплика после `interrupt` принимается в очередь глубины 1
# ---------------------------------------------------------------------------------------------


async def test_utterance_right_after_interrupt_is_queued_when_text_was_spoken(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Несущий кейс флагманского жеста, строка «непусто»: перебил и сразу заговорил.

    Кадра `turn_in_progress` НЕТ; первый ход закрывается своим `done`, следом идёт ход по
    отложенной реплике со СВОИМ `turnId`, и её `transcript` приходит ПОСЛЕ первого `done`.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.transcription.transcripts.extend(["первая реплика", "отложенная реплика"])
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    stand.script("Ответ на отложенную реплику. ")

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until(socket, "audio.end")
    first_turn = first_of(head, "transcript")["turnId"]

    await socket.send_frame({"type": "interrupt", "turnId": first_turn, "reason": "barge_in"})
    await socket.begin_utterance(audio=b"deferred-utterance")

    rest = await _read_until(socket, "done")
    assert [f for f in frames_of(rest, "error") if f["code"] == "turn_in_progress"] == []

    second = await _read_until(socket, "done")
    deferred_transcript = first_of(second, "transcript")
    assert deferred_transcript["text"] == "отложенная реплика"
    assert deferred_transcript["turnId"] != first_turn
    assert first_of(second, "done")["response"]["messageStepId"] == deferred_transcript["turnId"]


async def test_utterance_right_after_interrupt_is_queued_when_nothing_was_spoken(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Та же очередь в строке «пусто»: первый ход доходит до штатного конца, реплика ждёт.

    Именно здесь сегодняшний отказ стоил бы человеку всей длины ответа, которого он уже не
    слушает.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.transcription.transcripts.extend(["первая реплика", "отложенная реплика"])
    stand.script(barrier, *FIVE_SENTENCES)
    stand.script("Ответ на отложенную реплику. ")

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    transcript = await socket.next()
    await socket.send_frame(
        {"type": "interrupt", "turnId": transcript["turnId"], "reason": "user_stop"}
    )
    await socket.begin_utterance(audio=b"deferred-utterance")

    first = await _read_until(socket, "done")
    assert [f for f in frames_of(first, "error") if f["code"] == "turn_in_progress"] == []
    # Генерация не отменялась — ответ целиком.
    answer = first_of(first, "done")["response"]["assistantMessage"]
    for sentence in FIVE_SENTENCES:
        assert sentence.strip() in answer

    second = await _read_until(socket, "done")
    assert first_of(second, "transcript")["text"] == "отложенная реплика"


async def test_the_queue_slot_is_exactly_one(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обратная сторона: слот ОДИН — вторая реплика при занятом слоте получает отказ.

    Новых кодов под это не вводится: глубина 1 — следствие §3, а не произвол.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    stand.script("Ответ на отложенную реплику. ")

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until(socket, "audio.end")
    turn_id = first_of(head, "transcript")["turnId"]

    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "barge_in"})
    await socket.begin_utterance(audio=b"deferred-one")
    await socket.begin_utterance(audio=b"deferred-two")

    rest = await _read_until(socket, "done")
    rejected = [f for f in frames_of(rest, "error") if f["code"] == "turn_in_progress"]
    assert rejected, "вторая реплика при занятом слоте обязана получить `turn_in_progress`"
    assert rejected[0]["scope"] == "turn"


async def test_the_deferred_utterance_is_an_ordinary_turn_without_the_mark(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прерывание предыдущего хода на отложенную реплику НЕ распространяется.

    Её шаг пометки `interrupted` не несёт и кадра `interrupted` её ход не даёт.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    stand.script("Ответ на отложенную реплику целиком. ")

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until(socket, "audio.end")
    first_turn = first_of(head, "transcript")["turnId"]
    await socket.send_frame({"type": "interrupt", "turnId": first_turn, "reason": "barge_in"})
    await socket.begin_utterance(audio=b"deferred-utterance")
    await _read_until(socket, "done")
    second = await _read_until(socket, "done")

    assert frames_of(second, "interrupted") == []
    deferred_turn = first_of(second, "done")["response"]["messageStepId"]
    payloads = [
        _payload(step)
        for step in await chat_steps(stand, ready["sessionId"])
        if step["role"] == "assistant" and str(step["message_step_id"]) == deferred_turn
    ]
    assert payloads and all("interrupted" not in p for p in payloads)


async def test_the_deferred_utterance_passes_the_gates_at_start_not_at_queueing(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отложенная реплика проходит `_allow_turn` в момент СТАРТА, а не постановки (diff).

    Ось режима, снятая между постановкой и стартом, гасит её `voice_mode_disabled` + close
    `1000`. Падает на реализации, проверившей гейты один раз при приёме в очередь.
    """
    from app.instance_config.snapshot import reset_snapshot

    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    stand.script("Этот ответ не должен прозвучать. ")

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    head = await _read_until(socket, "audio.end")
    turn_id = first_of(head, "transcript")["turnId"]
    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "barge_in"})
    await socket.begin_utterance(audio=b"deferred-utterance")

    try:
        # Ось снимается ПОСЛЕ постановки в очередь и ДО старта отложенного хода.
        _voice_input_overlay(enabled=False)
        rest = await _read_until(socket, "done")
        assert first_of(rest, "done")["response"]["messageStepId"] == turn_id
        error = await socket.next()
        closed = await socket.next()
    finally:
        reset_snapshot()

    assert (error["code"], error["scope"]) == ("voice_mode_disabled", "session")
    assert closed["code"] == 1000
    assert SETTING_CHAT_VOICE_INPUT_ENABLED  # ось снималась именно оверлеем, а не env


# ---------------------------------------------------------------------------------------------
# 13.14 Последовательность ходов — по СЕССИИ, а не по соединению
# ---------------------------------------------------------------------------------------------


async def test_second_socket_on_the_same_session_cannot_start_a_turn(
    voice_stand: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Второй сокет по той же сессии при живом ходе (diff, §13.14).

    Соединение A начинает долгий ход и ОБРЫВАЕТСЯ; B поднимается с тем же `sessionId` и шлёт
    реплику ДО закрытия хода A. Проверяется НАБЛЮДАЕМЫЙ ФАКТ, а не лейбл: в истории порядок
    шагов остаётся `user(1), assistant(1)` — двух подряд user-шагов нет.

    Замок берётся ПО-НАСТОЯЩЕМУ: дублёр Redis фиксирует взятие и отказ, а отсутствие WARNING
    `voice_turn_lock_unavailable` доказывает, что ветка fail-open не срабатывала.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    release = asyncio.Event()

    async def _hold() -> None:
        await release.wait()

    stand.transcription.transcripts.extend(["реплика соединения A", "реплика соединения B"])
    stand.script(FIVE_SENTENCES[0], _hold, *FIVE_SENTENCES[1:])

    socket_a, ready = await stand.session(uid)
    session_id = ready["sessionId"]
    await socket_a.begin_utterance()
    await _read_until(socket_a, "delta")
    lock_key = f"voice:turn:{session_id}"
    assert stand.redis.held(lock_key), "нога A обязана держать межпроцессный признак"

    await socket_a.disconnect()

    # Тот же приём, что в кейсе §13.12, и здесь он ВАЖНЕЕ: ассерт проверяет ОТСУТСТВИЕ записи,
    # а на сломанном захвате отсутствие наступает само собой — кейс прошёл бы, ничего не доказав.
    target = logging.getLogger("app.api_gateway.routers.chat_voice")
    was_disabled, was_level = target.disabled, target.level
    target.disabled = False
    target.setLevel(logging.WARNING)
    target.addHandler(caplog.handler)
    try:
        socket_b = await stand.connect(uid)
        ready_b = await socket_b.start(sessionId=session_id)
        assert ready_b["type"] == "ready"
        await socket_b.begin_utterance(audio=b"second-connection")
        rejected = await socket_b.next()
    finally:
        target.removeHandler(caplog.handler)
        target.disabled, target.level = was_disabled, was_level

    assert (rejected["code"], rejected["scope"]) == ("turn_in_progress", "turn")
    denied = [c for c in stand.redis.calls if c["key"] == lock_key and not c["taken"]]
    assert denied, "отказ пришёл ОТ ЗАМКА: взятие ключа вернуло «занято»"
    assert not any(
        "voice_turn_lock_unavailable" in record.getMessage() for record in caplog.records
    ), "ветка fail-open не срабатывала — иначе кейс не проверял бы ничего"

    # Ход A доводится до конца, замок снимается, и та же реплика по B проходит нормально.
    release.set()
    await socket_a.aclose()
    assert not stand.redis.held(lock_key)

    stand.script("Ответ по соединению B. ")
    await socket_b.begin_utterance(audio=b"second-connection")
    frames_b = await _read_until(socket_b, "done")
    assert first_of(frames_b, "done")["response"]["status"] == "assistant_message"

    # Наблюдаемый факт: двух подряд шагов пользователя в истории нет.
    roles = [step["role"] for step in await chat_steps(stand, session_id)]
    assert "user" in roles
    for earlier, later in zip(roles, roles[1:], strict=False):
        assert not (earlier == "user" and later == "user"), roles


# ---------------------------------------------------------------------------------------------
# 13.15 `voice_mode_turns_total` — один инкремент на ХОД
# ---------------------------------------------------------------------------------------------


async def test_two_legged_turn_increments_the_series_once(voice_stand: Any) -> None:
    """Обычный ход с инструментом даёт `ok` +1, а не +2, и ОДНУ запись лога `voice_mode_turn`.

    Падает на инкременте в `finally` ноги: там искажается знаменатель «доли прерванных ходов».
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    before = _turns()

    socket, done, _ = await _tool_leg(stand, uid, "Сейчас посмотрю календарь. ")
    stand.script("Календарь посмотрел, отвечаю. ")
    await _send_tool_result(socket, done)
    final = await _read_until(socket, "done")

    assert first_of(final, "done")["response"]["status"] == "assistant_message"
    delta = _delta(before, _turns())
    assert delta["ok"] == 1, f"ход весит один инкремент, а не по одному на ногу: {delta}"
    assert sum(delta.values()) == 1


async def test_two_legged_turn_interrupted_increments_interrupted_once(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тот же ход, прерванный на ноге continuation, даёт `interrupted` +1.

    Искажался именно знаменатель «доли прерванных»: нога `tool_call` добавляла лишний `ok`.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    before = _turns()

    socket, done, _ = await _tool_leg(stand, uid, "Сейчас посмотрю календарь. ")
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    await _send_tool_result(socket, done)
    await _read_until(socket, "audio.end")
    await socket.send_frame(
        {"type": "interrupt", "turnId": done["messageStepId"], "reason": "barge_in"}
    )
    await _read_until(socket, "done")

    delta = _delta(before, _turns())
    assert delta["interrupted"] == 1
    assert delta["ok"] == 0, "нога `tool_call` хода не закрывала и в серию попасть не имеет права"
    assert sum(delta.values()) == 1


async def test_two_legged_turn_failing_on_continuation_counts_only_the_failure(
    voice_stand: Any,
) -> None:
    """Ход, успешный на первой ноге и упавший на continuation, даёт +1 `upstream_error` и 0 `ok`.

    Иначе числитель алерта завышается относительно счёта ходов.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    before = _turns()

    socket, done, _ = await _tool_leg(stand, uid, "Сейчас посмотрю календарь. ")
    stand.script(FIVE_SENTENCES[0], upstream_failure())
    await _send_tool_result(socket, done)
    await _read_until(socket, "error")

    delta = _delta(before, _turns())
    assert delta["upstream_error"] == 1
    assert delta["ok"] == 0
    assert sum(delta.values()) == 1


async def test_a_turn_abandoned_on_the_barrier_never_enters_the_series(
    voice_stand: Any,
) -> None:
    """Обратная сторона: ход, брошенный НА БАРЬЕРЕ, в серию не попадает вовсе.

    Терминальной ноги у него нет, и отдельного значения под этот исход не заводится: у него нет
    наблюдаемого сервером момента, а объявленное значение без производящего события — мёртвая
    декларация. Кейс фиксирует это как НОРМУ, а не как потерю.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    before = _turns()

    socket, _done, _ = await _tool_leg(stand, uid, "Сейчас посмотрю календарь. ")
    # Результаты инструмента не приходят никогда: устройство ушло.
    await socket.disconnect()
    await socket.aclose()

    assert _delta(before, _turns()) == dict.fromkeys(_TURN_OUTCOMES, 0)


# ---------------------------------------------------------------------------------------------
# 13.16 Рукопожатие расходует токен бакета ходов
# ---------------------------------------------------------------------------------------------


async def test_handshake_spends_a_chat_rate_limit_token(voice_stand: Any) -> None:
    """Цена сеанса — N+1 токенов (diff по величине): рукопожатие тратит токен ДО апгрейда.

    При `RATE_LIMIT_CHAT_PER_USER=2` первое рукопожатие проходит и один ход делает, а второе
    рукопожатие получает `429` — хотя ходов сделано МЕНЬШЕ лимита. Кейс фиксирует цену как
    норму: бакет общий с текстовым чатом, каждый реконнект сжигает ещё один токен. Падает, если
    рукопожатие перестанет расходовать токен — это сняло бы единственную границу темпа открытия
    сокетов.
    """
    stand = await voice_stand(RATE_LIMIT_CHAT_PER_USER="2")
    uid = await seed_voice_user(stand, balance=100)
    stand.script("Единственный ответ сеанса. ")

    socket, _ = await stand.session(uid)
    assert stand.chat_bucket.taken == 1, "токен потрачен уже рукопожатием"
    frames = await socket.turn()
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"
    assert stand.chat_bucket.taken == 2, "ход стоит второй токен: сеанс из N ходов = N+1"

    with pytest.raises(VoiceHandshakeDenied) as denied:
        await stand.connect(uid)

    assert denied.value.status == 429
    assert denied.value.payload["error"]["code"] == "rate_limited"
