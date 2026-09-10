"""Integration: тарификация потокового синтеза в голосовом режиме (ADR-104 §6, ADR-100 §9).

Норма — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`, раздел
«Integration — тарификация синтеза».

Единица списания — ОЗВУЧЕННЫЙ ШАГ ассистента, ключ `tts:{stepId}:{voiceId}` — тот же, что у
кнопки «прослушать». Отсюда все кейсы ниже: сегменты одного шага дают одну строку, ход с
инструментом — две, а кнопка на том же шаге получает `creditsCharged: 0`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import auth_headers
from tests.integration.test_voice_mode_session_adr104 import (
    balance_of,
    ledger_rows,
    seed_voice_user,
)
from tests.voice_harness import (
    FIVE_SENTENCES,
    first_of,
    frames_of,
    interrupt_barrier,
    upstream_failure,
)

_TOOL_CALL = ("calendar.read", {"start": "2026-09-10T00:00:00Z", "end": "2026-09-11T00:00:00Z"})


def tts_keys(ledger: list[dict[str, Any]]) -> list[str]:
    return [row["idempotency_key"] for row in ledger if row["idempotency_key"].startswith("tts:")]


async def _voice_turn(stand: Any, uid: uuid.UUID, *deltas: Any) -> dict[str, Any]:
    """Один голосовой ход целиком; возвращает `done.response`."""
    stand.script(*deltas)
    socket, _ = await stand.session(uid)
    frames = await socket.turn()
    return dict(first_of(frames, "done")["response"])


# ---------------------------------------------------------------------------------------------
# Единица списания
# ---------------------------------------------------------------------------------------------


async def test_five_segments_of_one_step_are_one_debit(voice_stand: Any) -> None:
    """Одно списание на озвученный шаг (diff): пять сегментов — ОДНА строка леджера.

    Падает на посегментном списании: там было бы пять строк, то есть один ответ превратился бы в
    пять списаний, а правило `ADR-100 §9` отменилось бы молча.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    response = await _voice_turn(stand, uid, *FIVE_SENTENCES)

    assert stand.speech.calls == len(FIVE_SENTENCES)
    ledger = await ledger_rows(stand, uid)
    keys = tts_keys(ledger)
    assert keys == [f"tts:{response['stepId']}:default_female"]
    charged = [row for row in ledger if row["idempotency_key"] == keys[0]]
    assert abs(charged[0]["amount"]) == 1  # TTS_CREDIT_COST стенда


async def test_unit_is_the_step_not_the_turn(voice_stand: Any) -> None:
    """Единица — ШАГ, а не ход (diff, обе стороны).

    Ход с клиентским инструментом, где озвучены и сопутствующий текст ноги `tool_call`, и финал,
    даёт ДВЕ строки — по одной на `stepId` каждой ноги. Падает на реализации с одним ключом на
    `messageStepId`: там вторая озвучка была бы бесплатной, а кнопка «прослушать» на том же шаге
    взяла бы деньги.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script_tool_call([_TOOL_CALL], "Сейчас посмотрю календарь и отвечу. ")

    socket, _ = await stand.session(uid)
    first_leg = await socket.turn()
    tool_done = first_of(first_leg, "done")["response"]
    assert tool_done["status"] == "tool_call"

    stand.script("Календарь посмотрел, отвечаю по существу. ")
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": tool_done["messageStepId"],
            "results": [{"toolCallId": tool_done["toolCalls"][0]["id"], "result": {"ok": True}}],
        }
    )
    final_leg = await socket.collect_until("done")
    final_done = first_of(final_leg, "done")["response"]

    keys = tts_keys(await ledger_rows(stand, uid))
    assert sorted(keys) == sorted(
        [
            f"tts:{tool_done['stepId']}:default_female",
            f"tts:{final_done['stepId']}:default_female",
        ]
    )
    assert tool_done["stepId"] != final_done["stepId"]


async def test_silent_tool_leg_gives_one_debit(voice_stand: Any) -> None:
    """Обратная сторона: нога `tool_call` без текста озвучивать нечего → ОДНА строка."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script_tool_call([_TOOL_CALL])

    socket, _ = await stand.session(uid)
    first_leg = await socket.turn()
    tool_done = first_of(first_leg, "done")["response"]
    assert frames_of(first_leg, "audio.end") == []

    stand.script("Календарь посмотрел, отвечаю по существу. ")
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": tool_done["messageStepId"],
            "results": [{"toolCallId": tool_done["toolCalls"][0]["id"], "result": {"ok": True}}],
        }
    )
    final_done = first_of(await socket.collect_until("done"), "done")["response"]

    assert tts_keys(await ledger_rows(stand, uid)) == [f"tts:{final_done['stepId']}:default_female"]


async def test_key_is_built_from_step_id_not_message_step_id(voice_stand: Any) -> None:
    """Ключ строится из `stepId`, а не из `messageStepId` (diff по значению).

    В ходе с инструментом эти величины РАЗЛИЧНЫ, поэтому подмена пространства идентификаторов
    здесь наблюдаема, а на обычном ходе была бы невидима.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script_tool_call([_TOOL_CALL], "Сейчас посмотрю календарь и отвечу. ")

    socket, _ = await stand.session(uid)
    done = first_of(await socket.turn(), "done")["response"]

    assert done["stepId"] != done["messageStepId"]
    keys = tts_keys(await ledger_rows(stand, uid))
    assert keys == [f"tts:{done['stepId']}:default_female"]
    assert f"tts:{done['messageStepId']}:default_female" not in keys


# ---------------------------------------------------------------------------------------------
# Момент списания
# ---------------------------------------------------------------------------------------------


async def test_interrupted_listening_is_still_paid(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Момент списания — закрытие озвученного шага: к `done` прерванного хода списание ЕСТЬ."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    frames: list[dict[str, Any]] = []
    while not frames or frames[-1]["type"] != "audio.end":
        frames.append(await socket.next())
    turn_id = first_of(frames, "transcript")["turnId"]
    await socket.send_frame({"type": "interrupt", "turnId": turn_id, "reason": "barge_in"})
    done = first_of(await socket.collect_until("done"), "done")["response"]

    assert tts_keys(await ledger_rows(stand, uid)) == [f"tts:{done['stepId']}:default_female"]


async def test_synthesizer_failing_on_the_first_segment_is_not_paid(voice_stand: Any) -> None:
    """Обратная сторона момента: отказ на ПЕРВОМ сегменте → списания нет вовсе.

    Ни дебита, ни возврата: порядок «синтез → списание» сохранён.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.speech.fail_on = 1

    await _voice_turn(stand, uid, *FIVE_SENTENCES)

    assert tts_keys(await ledger_rows(stand, uid)) == []


# ---------------------------------------------------------------------------------------------
# Отказы поставщиков и деньги
# ---------------------------------------------------------------------------------------------


async def test_llm_failure_does_not_pay_for_delivered_speech(voice_stand: Any) -> None:
    """Отказ LLM-провайдера синтез не оплачивает (diff): строк `tts:` нет ни одной.

    Падает на реализации, списывающей по `stepId` шага-пометки: там кнопка «прослушать» на нём
    отдала бы синтез строки «ход не удался» бесплатно.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(FIVE_SENTENCES[0], FIVE_SENTENCES[1], upstream_failure())

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    frames = await socket.collect_until("error")

    error = first_of(frames, "error")
    assert (error["code"], error["scope"]) == ("upstream_error", "turn")
    assert frames_of(frames, "audio.end"), "сегменты были доставлены до отказа"
    assert tts_keys(await ledger_rows(stand, uid)) == []


async def test_synthesizer_failing_mid_delivery_is_paid_once(voice_stand: Any) -> None:
    """Отказ СИНТЕЗАТОРА после доставленного сегмента синтез ОПЛАЧИВАЕТ (diff по величине).

    Падает на реализации, читающей «отказ синтезатора» как безусловное освобождение от
    списания: там доставленный звук остался бы неоплаченным.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.speech.fail_on = 3

    stand.script(*FIVE_SENTENCES)
    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    speech_errors = [f for f in frames_of(frames, "error") if f["scope"] == "speech"]
    assert speech_errors, "отказ синтезатора обязан прийти кадром scope:'speech'"
    assert len(frames_of(frames, "audio.end")) == 2
    done = first_of(frames, "done")["response"]
    for sentence in FIVE_SENTENCES:
        assert sentence.strip() in done["assistantMessage"]
    assert tts_keys(await ledger_rows(stand, uid)) == [f"tts:{done['stepId']}:default_female"]


async def test_after_a_paid_partial_delivery_the_button_is_free(voice_stand: Any) -> None:
    """Против переоценки: заплатив один раз, пользователь получает ПОЛНУЮ озвучку бесплатно.

    Падает на реализации, где отказ синтезатора не записал ключ: там пользователь заплатил бы
    второй раз за один и тот же ответ.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.speech.fail_on = 3

    stand.script(*FIVE_SENTENCES)
    socket, ready = await stand.session(uid)
    done = first_of(await socket.turn(), "done")["response"]
    balance_after_turn = await balance_of(stand, uid)

    replay = await stand.http.post(
        "/v1/chat/speech",
        json={
            "userId": str(uid),
            "sessionId": ready["sessionId"],
            "stepId": done["stepId"],
        },
        headers=auth_headers(uid),
    )
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["creditsCharged"] == 0
    assert body["voiceId"] == "default_female"
    assert body["audio"]
    assert await balance_of(stand, uid) == balance_after_turn


# ---------------------------------------------------------------------------------------------
# Общий ключ с кнопкой «прослушать»
# ---------------------------------------------------------------------------------------------


async def test_button_after_voice_mode_is_free(voice_stand: Any) -> None:
    """Общий ключ с кнопкой (несущее следствие, diff): голос → кнопка = `creditsCharged: 0`."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    stand.script(*FIVE_SENTENCES)
    socket, ready = await stand.session(uid)
    done = first_of(await socket.turn(), "done")["response"]
    balance_after_turn = await balance_of(stand, uid)

    replay = await stand.http.post(
        "/v1/chat/speech",
        json={"userId": str(uid), "sessionId": ready["sessionId"], "stepId": done["stepId"]},
        headers=auth_headers(uid),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["creditsCharged"] == 0
    assert await balance_of(stand, uid) == balance_after_turn
    assert len(tts_keys(await ledger_rows(stand, uid))) == 1


async def test_voice_mode_after_the_button_is_free_too(voice_stand: Any) -> None:
    """Обратный порядок: сначала кнопка, затем голосовой режим на том же шаге — второй раз нет.

    Падает, если у путей разные ключи.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    # Ход по HTTP, чтобы кнопка была ПЕРВОЙ.
    stand.script(*FIVE_SENTENCES)
    http_turn = await stand.http.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "расскажи", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert http_turn.status_code == 200, http_turn.text
    turn = http_turn.json()
    button = await stand.http.post(
        "/v1/chat/speech",
        json={"userId": str(uid), "sessionId": turn["sessionId"], "stepId": turn["stepId"]},
        headers=auth_headers(uid),
    )
    assert button.status_code == 200, button.text
    assert button.json()["creditsCharged"] == 1
    keys_after_button = tts_keys(await ledger_rows(stand, uid))
    assert keys_after_button == [f"tts:{turn['stepId']}:default_female"]

    # Тот же шаг, тот же голос — второго списания не появляется.
    replay = await stand.http.post(
        "/v1/chat/speech",
        json={"userId": str(uid), "sessionId": turn["sessionId"], "stepId": turn["stepId"]},
        headers=auth_headers(uid),
    )
    assert replay.json()["creditsCharged"] == 0
    assert tts_keys(await ledger_rows(stand, uid)) == keys_after_button


async def test_changing_the_voice_creates_a_second_key(voice_stand: Any) -> None:
    """Смена голоса — новый ключ: та же пара после `PATCH /v1/preferences` даёт вторую строку."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    stand.script(*FIVE_SENTENCES)
    socket, ready = await stand.session(uid)
    done = first_of(await socket.turn(), "done")["response"]

    patched = await stand.http.patch(
        "/v1/preferences",
        json={"defaultVoiceId": "default_male"},
        headers=auth_headers(uid),
    )
    assert patched.status_code == 200, patched.text

    replay = await stand.http.post(
        "/v1/chat/speech",
        json={"userId": str(uid), "sessionId": ready["sessionId"], "stepId": done["stepId"]},
        headers=auth_headers(uid),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["voiceId"] == "default_male"
    assert replay.json()["creditsCharged"] == 1
    assert sorted(tts_keys(await ledger_rows(stand, uid))) == sorted(
        [f"tts:{done['stepId']}:default_female", f"tts:{done['stepId']}:default_male"]
    )


async def test_price_of_speech_and_price_of_the_turn_are_different_rows(
    voice_stand: Any,
) -> None:
    """Значение цены различает пути (diff по величине): `TTS_CREDIT_COST=3` при цене хода 1."""
    stand = await voice_stand(TTS_CREDIT_COST="3")
    uid = await seed_voice_user(stand, balance=100)

    done = await _voice_turn(stand, uid, *FIVE_SENTENCES)

    ledger = await ledger_rows(stand, uid)
    debits = {row["idempotency_key"]: abs(row["amount"]) for row in ledger}
    assert debits[f"tts:{done['stepId']}:default_female"] == 3
    assert debits[done["messageStepId"]] == 1
    assert await balance_of(stand, uid) == 100 - 4
