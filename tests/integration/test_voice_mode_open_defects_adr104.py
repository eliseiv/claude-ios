"""Integration: три кейса, ОЖИДАЕМО падающие на текущем коде (ADR-104, круг ревью 2).

Это `follow_up_for_qa` от `backend-reviewer`: три `major`, по которым поведение прод-кода будет
изменено. Кейсы написаны ПОД БУДУЩЕЕ, нормативное поведение и на коммите `00fd6d2` падают — их
падение и есть доказательство, что находки реальны. После фикса они обязаны позеленеть БЕЗ
переписывания.

Ни один из трёх не помечен `xfail`: помеченный кейс молчит, а молчащий кейс не отличается от
несуществующего — ровно та форма, из-за которой находки и теряются между кругами.

Опорные места действующего кода:
* (а) `src/app/chat/orchestrator.py:2593-2606` — ветка `except Exception → _mark_turn_failed`
  ноги continuation; `src/app/chat/orchestrator.py:1946` — гейт `has_assistant_step`, из-за
  которого пометка не пишется никогда.
* (б-1) и (б-2) `src/app/api_gateway/routers/chat_voice.py:977-983` — предикат `_settle_speech`
  не различает причины нуля доставленных сегментов и в двух из них отправляет `nothing_to_speak`,
  тогда как контракт (`docs/modules/chat-orchestrator/02-api-contracts.md:1001`) определяет эту
  причину дословно как «Очищенный текст ответа пуст».
"""

from __future__ import annotations

from typing import Any

from tests.integration.test_voice_mode_interrupt_adr104 import _payload
from tests.integration.test_voice_mode_session_adr104 import (
    chat_steps,
    seed_voice_user,
)
from tests.voice_harness import (
    FIVE_SENTENCES,
    first_of,
    frames_of,
    upstream_failure,
)

_TOOL_CALL = ("calendar.read", {"start": "2026-09-10T00:00:00Z", "end": "2026-09-11T00:00:00Z"})


async def _tool_call_leg(stand: Any, uid: Any, *deltas: Any) -> tuple[Any, dict[str, Any]]:
    """Первая нога хода с клиентским инструментом; возвращает сокет и её `done.response`."""
    stand.script_tool_call([_TOOL_CALL], *deltas)
    socket, ready = await stand.session(uid)
    frames = await socket.turn()
    done = first_of(frames, "done")["response"]
    assert done["status"] == "tool_call"
    return socket, {**done, "sessionId": ready["sessionId"]}


async def test_provider_failure_on_the_continuation_leg_marks_the_turn_failed(
    voice_stand: Any,
) -> None:
    """(а) Отказ провайдера на ноге continuation обязан закрыть ход пометкой `turnFailed`.

    Инвариант «транспорт можно оборвать в любой момент, ход — нельзя» — свойство ХОДА, а не ноги:
    шаг пользователя закоммичен ещё первой ногой, поэтому ход, брошенный на continuation, точно
    так же оставляет реплику без ответа, и на следующем ходу модель отвечает на неё.

    Сегодня пометка не появляется: `_mark_turn_failed` открывается гейтом `has_assistant_step`
    (`orchestrator.py:1946`), а нога `tool_call` уже записала assistant-шаг с тем же
    `message_step_id`, поэтому гейт всегда истинен и ветка `orchestrator.py:2593-2606`
    недостижима.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    socket, tool_done = await _tool_call_leg(stand, uid, "Сейчас посмотрю календарь. ")

    stand.script(FIVE_SENTENCES[0], upstream_failure())
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": tool_done["messageStepId"],
            "results": [{"toolCallId": tool_done["toolCalls"][0]["id"], "result": {"ok": True}}],
        }
    )
    frames = await socket.collect_until("error")

    error = first_of(frames, "error")
    assert (error["code"], error["scope"]) == ("upstream_error", "turn")

    steps = await chat_steps(stand, tool_done["sessionId"])
    turn_steps = [
        step
        for step in steps
        if step["role"] == "assistant"
        and str(step["message_step_id"]) == tool_done["messageStepId"]
    ]
    marked = [step for step in turn_steps if "turnFailed" in _payload(step)]
    assert marked, (
        "ход, оборванный на ноге continuation, обязан нести пометку `turnFailed` — иначе реплика "
        "остаётся без ответа, и следующий ход отвечает на прошлую"
    )


async def test_speech_rate_limit_does_not_masquerade_as_nothing_to_speak(
    voice_stand: Any,
) -> None:
    """(б-1) Бакет синтеза исчерпан до первого сегмента → это НЕ «нечего произносить».

    Контракт определяет `nothing_to_speak` дословно как «Очищенный текст ответа пуст»
    (`02-api-contracts.md:1001`), а текст здесь не пуст: клиент, получив эту причину, покажет
    пользователю заведомо ложное объяснение молчания. Наблюдаемая причина молчания — отказ
    бакета, и она уже уходит кадром `error {code:"rate_limited", scope:"speech"}`.
    """
    from app.api_gateway.routers import chat_voice

    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    async def _deny(**_kwargs: Any) -> bool:
        return False

    original = chat_voice.enforce_speech_limits
    chat_voice.enforce_speech_limits = _deny  # type: ignore[assignment]
    try:
        stand.script(*FIVE_SENTENCES)
        socket, _ = await stand.session(uid)
        frames = await socket.turn()
    finally:
        chat_voice.enforce_speech_limits = original  # type: ignore[assignment]

    rate_limited = [
        frame
        for frame in frames_of(frames, "error")
        if frame["code"] == "rate_limited" and frame["scope"] == "speech"
    ]
    assert rate_limited, "исчерпанный бакет синтеза обязан быть назван своим кадром"
    assert stand.speech.calls == 0
    skipped = frames_of(frames, "speech.skipped")
    assert skipped == [], (
        "причина `nothing_to_speak` здесь заведомо ложна: очищенный текст ответа не пуст, "
        f"молчание вызвано отказом бакета; пришло {skipped}"
    )


async def test_budget_exhausted_by_the_first_leg_is_not_nothing_to_speak(
    voice_stand: Any,
) -> None:
    """(б-2) Потолок исчерпан ПЕРВОЙ ногой → на второй ноге `nothing_to_speak` не приходит.

    Текст второй ноги не пуст — молчание вызвано совокупным потолком хода, о котором клиент уже
    узнал признаком `truncated: true` на последнем доставленном сегменте. Ложная причина в
    `speech.skipped` объясняет молчание неверно.
    """
    stand = await voice_stand(TTS_MAX_CHARS="40")
    uid = await seed_voice_user(stand, balance=100)
    socket, tool_done = await _tool_call_leg(
        stand, uid, "Сейчас посмотрю календарь и сразу отвечу по существу. "
    )
    first_leg_ends = stand.speech.calls
    assert first_leg_ends >= 1, "первая нога обязана израсходовать бюджет"

    stand.script("Календарь посмотрел, отвечаю по существу подробно и обстоятельно. ")
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": tool_done["messageStepId"],
            "results": [{"toolCallId": tool_done["toolCalls"][0]["id"], "result": {"ok": True}}],
        }
    )
    second_leg = await socket.collect_until("done")

    assert frames_of(second_leg, "audio.end") == [], "бюджет хода исчерпан первой ногой"
    skipped = frames_of(second_leg, "speech.skipped")
    assert skipped == [], (
        "причина `nothing_to_speak` здесь заведомо ложна: текст второй ноги не пуст, молчание "
        f"вызвано совокупным потолком хода; пришло {skipped}"
    )
