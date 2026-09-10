"""Integration: нормы, вскрытые первой реализацией голосового режима (ADR-104 §13).

Состав кейсов — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим` (разделы,
добавленные тем же проходом, что и §13). Здесь собрано то, что §1–§12 не описывали и что §13
закрыл РЕШЕНИЕМ: предикат закрытия хода на ноге continuation, единица бакета `rl:speech`, резолв
голоса на сеанс, момент создания сессии и заголовка, две точки потолка реплики, приостановка
idle-таймаута, кадры протокола, точное исчерпание бюджета, ось на каждом ходу и колбэк
`on_turn_start`.

⚠️ Пороги кейсы задают САМИ (врезка того же раздела): на харнессном значении кейс о пороге зелен
при любой реализации.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from typing import Any

import pytest

from app.instance_config.settings_registry import SETTING_CHAT_VOICE_INPUT_ENABLED
from app.instance_config.snapshot import (
    InstanceConfigSnapshot,
    SettingOverlay,
    install_snapshot,
    reset_snapshot,
)
from tests.conftest import auth_headers
from tests.integration.test_voice_mode_billing_adr104 import tts_keys
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
    upstream_failure,
)

_TOOL_CALL = ("calendar.read", {"start": "2026-09-10T00:00:00Z", "end": "2026-09-11T00:00:00Z"})


def _payload(step: dict[str, Any]) -> dict[str, Any]:
    raw = step["payload"]
    return raw if isinstance(raw, dict) else json.loads(raw)


def _assistant(steps: list[dict[str, Any]], turn_id: str) -> list[dict[str, Any]]:
    return [
        step
        for step in steps
        if step["role"] == "assistant" and str(step["message_step_id"]) == turn_id
    ]


async def _tool_leg(stand: Any, uid: uuid.UUID, *deltas: Any) -> tuple[Any, dict[str, Any], str]:
    """Первая нога хода с клиентским инструментом: сокет, `done.response`, `sessionId`."""
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


async def _read_until_first_audio_end(socket: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while not frames or frames[-1]["type"] != "audio.end":
        frames.append(await socket.next())
    return frames


# ---------------------------------------------------------------------------------------------
# 13.1 Закрытие хода на ноге `continuation` — предикат «завершающего шага»
# (несущий кейс живёт в test_voice_mode_open_defects_adr104.py: он и был находкой ревью)
# ---------------------------------------------------------------------------------------------


async def test_http_tool_result_marks_the_turn_failed_too(voice_stand: Any) -> None:
    """Тот же путь на HTTP (diff по транспорту): норма — свойство ХОДА, а не сокета.

    На `POST /v1/chat/tool-result` это ИЗМЕНЕНИЕ предсуществующего поведения. Тест падает, если
    пометку научили писать только сокет.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script_tool_call([_TOOL_CALL], "Сейчас посмотрю календарь. ")
    run = await stand.http.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "посмотри календарь", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert run.status_code == 200, run.text
    turn = run.json()
    assert turn["status"] == "tool_call"

    stand.llm.raise_upstream = True
    failed = await stand.http.post(
        "/v1/chat/v2/tool-result",
        json={
            "userId": str(uid),
            "sessionId": turn["sessionId"],
            "results": [{"toolCallId": turn["toolCalls"][0]["id"], "result": {"ok": True}}],
        },
        headers=auth_headers(uid),
    )
    stand.llm.raise_upstream = False
    assert failed.status_code >= 500, failed.text

    steps = await chat_steps(stand, turn["sessionId"])
    marked = [s for s in _assistant(steps, turn["messageStepId"]) if "turnFailed" in _payload(s)]
    assert marked, "ход, оборванный на continuation, обязан нести пометку и на HTTP-пути"


async def test_successful_continuation_has_exactly_one_closing_step_and_no_marks(
    voice_stand: Any,
) -> None:
    """Против переоценки: двух закрывающих шагов не бывает, пометок нет ни одной."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    socket, done, session_id = await _tool_leg(stand, uid, "Сейчас посмотрю календарь. ")

    stand.script("Календарь посмотрел, отвечаю. ")
    await _send_tool_result(socket, done)
    final = first_of(await socket.collect_until("done"), "done")["response"]
    assert final["status"] == "assistant_message"

    steps = await chat_steps(stand, session_id)
    payloads = [_payload(step) for step in _assistant(steps, done["messageStepId"])]
    closing = [p for p in payloads if not p.get("toolCalls") and "tool_use" not in json.dumps(p)]
    assert len(closing) == 1, "ровно один завершающий шаг ассистента"
    assert all("turnFailed" not in p and "interrupted" not in p for p in payloads)


async def test_interrupted_continuation_leg_carries_interrupted_not_turn_failed(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прерывание ноги `continuation` (diff): предикат §5 действует на ЛЮБОЙ ноге хода.

    Падает на реализации, где предикат живёт только на первой ноге: там нога continuation
    закрылась бы как обычная — без кадра `interrupted` и без пометки.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    socket, done, session_id = await _tool_leg(stand, uid, "Сейчас посмотрю календарь. ")

    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    await _send_tool_result(socket, done)
    head = await _read_until_first_audio_end(socket)
    await socket.send_frame(
        {"type": "interrupt", "turnId": done["messageStepId"], "reason": "barge_in"}
    )
    tail = await socket.collect_until("done")

    assert frames_of(tail, "interrupted"), "кадр `interrupted` обязан прийти и на ноге continuation"
    steps = await chat_steps(stand, session_id)
    payloads = [_payload(step) for step in _assistant(steps, done["messageStepId"])]
    assert any("interrupted" in p for p in payloads)
    assert all("turnFailed" not in p for p in payloads)
    assert delta_text(head + tail), "префикс ноги continuation был накоплен"
    ledger = await ledger_rows(stand, uid)
    assert done["messageStepId"] in [row["idempotency_key"] for row in ledger], "ход списан"


async def test_replay_after_a_failed_continuation_calls_the_provider_again(
    voice_stand: Any,
) -> None:
    """Реплей после отказа зовёт провайдера ЗАНОВО (diff, несущий).

    Падает на реализации, где `next_step_after` возвращает пометку как сохранённый виток: там
    повтор после транзиентного отказа навсегда отдавал бы «ход не удался».
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    socket, done, _ = await _tool_leg(stand, uid, "Сейчас посмотрю календарь. ")

    stand.script(FIVE_SENTENCES[0], upstream_failure())
    await _send_tool_result(socket, done)
    await socket.collect_until("error")
    calls_after_failure = len(stand.llm.calls)

    stand.script("Со второй попытки отвечаю по существу. ")
    await _send_tool_result(socket, done)
    replay = await socket.collect_until("done")

    assert len(stand.llm.calls) == calls_after_failure + 1, "повтор обязан звать провайдера заново"
    response = first_of(replay, "done")["response"]
    assert response["status"] == "assistant_message"
    assert "не удал" not in (response["assistantMessage"] or "").lower()


async def test_replay_after_an_interrupted_leg_returns_the_saved_step(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обратная сторона: реплей после ПРЕРВАННОЙ ноги отдаёт сохранённый шаг, провайдера не зовёт.

    Пометка прерывания — настоящий ответ, и пропускать её нельзя.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    socket, done, _ = await _tool_leg(stand, uid, "Сейчас посмотрю календарь. ")

    barrier = interrupt_barrier(monkeypatch)
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    await _send_tool_result(socket, done)
    await _read_until_first_audio_end(socket)
    await socket.send_frame(
        {"type": "interrupt", "turnId": done["messageStepId"], "reason": "barge_in"}
    )
    await socket.collect_until("done")
    calls_after_interrupt = len(stand.llm.calls)

    await _send_tool_result(socket, done)
    await socket.collect_until("done")

    assert len(stand.llm.calls) == calls_after_interrupt, "провайдер повторно не зовётся"


# ---------------------------------------------------------------------------------------------
# 13.2 Бакет `rl:speech` — единица, точка применения, кадр отказа
# ---------------------------------------------------------------------------------------------


async def test_speech_bucket_silences_the_second_step_and_keeps_the_turn(
    voice_stand: Any,
) -> None:
    """Бакет исчерпан: первый озвученный шаг звучит, второй — `rate_limited` со `scope:"speech"`.

    Ход при этом идёт и списывается, соединение живо, close-кадра нет, а строки леджера `tts:`
    у замолчавшего шага нет — синтез оплачивается по общему условию, а доставлено не было ничего.
    """
    stand = await voice_stand(TTS_RATE_LIMIT_PER_MIN="1")
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    first = await socket.turn()
    assert frames_of(first, "audio.end"), "первый шаг обязан прозвучать"
    calls_after_first = stand.speech.calls

    second = await socket.turn()

    limited = [f for f in frames_of(second, "error") if f["scope"] == "speech"]
    assert limited and limited[0]["code"] == "rate_limited"
    assert stand.speech.calls == calls_after_first, "синтезатор второго шага не вызван ни разу"
    assert frames_of(second, "audio.end") == []
    done = first_of(second, "done")["response"]
    assert done["status"] == "assistant_message"
    assert done["assistantMessage"] == delta_text(second)
    assert socket.close_code is None
    ledger = await ledger_rows(stand, uid)
    assert done["messageStepId"] in [row["idempotency_key"] for row in ledger], "ход списан"
    assert f"tts:{done['stepId']}:default_female" not in tts_keys(ledger)


async def test_one_token_buys_a_whole_answer_not_a_segment(voice_stand: Any) -> None:
    """Обратная сторона: девять сегментов одного шага тратят ОДИН токен бакета.

    Падает на посегментном взятии токена: там девять сегментов исчерпали бы бакет из десяти
    почти целиком, и вторая половина ответа замолчала бы.
    """
    nine = [f"Предложение номер {n} этого ответа. " for n in range(1, 10)]
    stand = await voice_stand(TTS_RATE_LIMIT_PER_MIN="10")
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*nine)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    assert len(frames_of(frames, "audio.end")) == len(nine), "ответ обязан прозвучать целиком"
    assert [f for f in frames_of(frames, "error") if f["scope"] == "speech"] == []
    assert stand.speech_bucket.taken == 1, "один озвученный шаг — один токен"


async def test_bucket_unit_is_the_step_on_a_two_legged_turn(voice_stand: Any) -> None:
    """Единица бакета — озвученный ШАГ (diff по величине), а не ход.

    Падает на реализации «один токен на ход»: там вторая озвучка того же хода прошла бы мимо
    защиты.
    """
    stand = await voice_stand(TTS_RATE_LIMIT_PER_MIN="1")
    uid = await seed_voice_user(stand, balance=100)
    socket, done, _ = await _tool_leg(stand, uid, "Сейчас посмотрю календарь и отвечу. ")
    assert stand.speech.calls >= 1, "нога tool_call обязана прозвучать"

    stand.script("Календарь посмотрел, отвечаю по существу. ")
    calls_after_first_leg = stand.speech.calls
    await _send_tool_result(socket, done)
    second_leg = await socket.collect_until("done")

    limited = [f for f in frames_of(second_leg, "error") if f["scope"] == "speech"]
    assert limited and limited[0]["code"] == "rate_limited"
    assert stand.speech.calls == calls_after_first_leg


async def test_bucket_is_shared_with_the_listen_button(voice_stand: Any) -> None:
    """Бакет общий с кнопкой (несущее следствие): второго бакета не заводится.

    Исчерпав его прослушиваниями `POST /v1/chat/speech`, пользователь получает на голосовом ходе
    `rate_limited` со `scope:"speech"`.
    """
    stand = await voice_stand(TTS_RATE_LIMIT_PER_MIN="1")
    uid = await seed_voice_user(stand, balance=100)

    stand.script("Ответ, который слушают кнопкой. ")
    run = await stand.http.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "привет", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert run.status_code == 200, run.text
    turn = run.json()
    button = await stand.http.post(
        "/v1/chat/speech",
        json={"userId": str(uid), "sessionId": turn["sessionId"], "stepId": turn["stepId"]},
        headers=auth_headers(uid),
    )
    assert button.status_code == 200, button.text

    stand.script(*FIVE_SENTENCES)
    socket, _ = await stand.session(uid, sessionId=turn["sessionId"])
    frames = await socket.turn()

    limited = [f for f in frames_of(frames, "error") if f["scope"] == "speech"]
    assert limited and limited[0]["code"] == "rate_limited"
    assert frames_of(frames, "audio.end") == []


async def test_voice_mode_consumes_the_bucket_the_button_sees(voice_stand: Any) -> None:
    """…и наоборот: после голосовой озвучки ручка отвечает `429`."""
    stand = await voice_stand(TTS_RATE_LIMIT_PER_MIN="1")
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)

    socket, ready = await stand.session(uid)
    done = first_of(await socket.turn(), "done")["response"]
    assert stand.speech_bucket.taken == 1

    button = await stand.http.post(
        "/v1/chat/speech",
        json={"userId": str(uid), "sessionId": ready["sessionId"], "stepId": done["stepId"]},
        headers=auth_headers(uid),
    )
    assert button.status_code == 429, button.text
    assert button.json()["error"]["code"] == "rate_limited"


async def test_rate_limited_of_two_scopes_is_not_collapsed(voice_stand: Any) -> None:
    """`rate_limited` двух областей не схлопнут (diff по `scope`): один `code`, разные исходы.

    `scope:"turn"` — ход НЕ начался; `scope:"speech"` — ход идёт и списывается.
    """
    from app.api_gateway.routers import chat_voice

    stand = await voice_stand(TTS_RATE_LIMIT_PER_MIN="1")
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    await socket.turn()

    # (а) бакет синтеза: ход идёт и списывается.
    speech_turn = await socket.turn()
    speech_error = [f for f in frames_of(speech_turn, "error") if f["scope"] == "speech"][0]
    speech_done = first_of(speech_turn, "done")["response"]
    ledger_keys = [row["idempotency_key"] for row in await ledger_rows(stand, uid)]
    assert speech_done["messageStepId"] in ledger_keys

    # (б) лимит ходов: ход не начался вовсе.
    async def _deny(**_kwargs: Any) -> bool:
        return False

    original = chat_voice.enforce_chat_limits
    chat_voice.enforce_chat_limits = _deny  # type: ignore[assignment]
    try:
        calls_before = len(stand.llm.calls)
        await socket.begin_utterance()
        turn_frames = await socket.collect_until("error")
    finally:
        chat_voice.enforce_chat_limits = original  # type: ignore[assignment]

    turn_error = first_of(turn_frames, "error")
    assert speech_error["code"] == turn_error["code"] == "rate_limited"
    assert (speech_error["scope"], turn_error["scope"]) == ("speech", "turn")
    assert len(stand.llm.calls) == calls_before, "ход со `scope:turn` не начинается"


# ---------------------------------------------------------------------------------------------
# 13.3 `voiceId` — резолв на СЕАНС
# ---------------------------------------------------------------------------------------------


async def test_voice_id_is_fixed_for_the_whole_session(voice_stand: Any) -> None:
    """`voiceId` фиксируется на СЕАНС (diff, обе стороны).

    Смена настройки между ходами одного соединения голоса НЕ меняет: `ready` уже назвал голос
    сеанса, и расхождение с ним было бы величиной, которую сервер сам же опроверг. После
    переподключения новый `ready.voiceId` равен новой настройке.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script("Первый ответ сеанса. ")
    stand.script("Второй ответ того же сеанса. ")

    socket, ready = await stand.session(uid)
    first = await socket.turn()
    assert first_of(first, "audio.begin")["voiceId"] == ready["voiceId"] == "default_female"

    patched = await stand.http.patch(
        "/v1/preferences",
        json={"defaultVoiceId": "default_male"},
        headers=auth_headers(uid),
    )
    assert patched.status_code == 200, patched.text

    second = await socket.turn()
    assert first_of(second, "audio.begin")["voiceId"] == ready["voiceId"]
    assert first_of(second, "audio.begin")["voiceId"] != "default_male"

    # После переподключения сеанс берёт новую настройку.
    stand.script("Ответ нового сеанса. ")
    fresh_socket, fresh_ready = await stand.session(uid, sessionId=ready["sessionId"])
    assert fresh_ready["voiceId"] == "default_male"
    fresh = await fresh_socket.turn()
    assert first_of(fresh, "audio.begin")["voiceId"] == "default_male"


# ---------------------------------------------------------------------------------------------
# 13.4 Момент создания сессии и автозаголовок
# ---------------------------------------------------------------------------------------------


async def test_start_creates_the_session_before_any_utterance(voice_stand: Any) -> None:
    """`start` без `sessionId` создаёт сессию сразу: она видна в списке БЕЗ заголовка и без шагов.

    Названное следствие §13.4: на HTTP-пути такого состояния не бывает — там сессия рождается
    вместе с первым сообщением.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)

    _, ready = await stand.session(uid)

    listing = await stand.http.get("/v1/chats", headers=auth_headers(uid))
    assert listing.status_code == 200, listing.text
    rows = [row for row in listing.json()["items"] if row["id"] == ready["sessionId"]]
    assert rows, "сессия обязана быть видна сразу после `start`"
    assert rows[0]["title"] is None

    # Шагов нет ни одного, и проверяется это ИСТОРИЕЙ (список чатов шаги не отдаёт вовсе).
    history = await stand.http.get(f"/v1/chats/{ready['sessionId']}", headers=auth_headers(uid))
    assert history.status_code == 200, history.text
    assert history.json()["steps"] == []
    assert await chat_steps(stand, ready["sessionId"]) == []


async def test_title_comes_from_the_first_utterance_and_never_changes(voice_stand: Any) -> None:
    """Заголовок — `derive_title` ПЕРВОЙ реплики, вторая его не переписывает (идемпотентно).

    Падает и на реализации, проставляющей заголовок при создании сессии, и на реализации,
    переписывающей его каждой репликой.
    """
    from app.chat.repository import derive_title

    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.transcription.transcripts.extend(["первая реплика сеанса", "вторая реплика сеанса"])
    stand.script("Ответ на первую. ")
    stand.script("Ответ на вторую. ")

    socket, ready = await stand.session(uid)
    await socket.turn()

    async def _title() -> str | None:
        listing = await stand.http.get("/v1/chats", headers=auth_headers(uid))
        row = next(r for r in listing.json()["items"] if r["id"] == ready["sessionId"])
        return row["title"]

    assert await _title() == derive_title("первая реплика сеанса")

    await socket.turn()
    assert await _title() == derive_title("первая реплика сеанса")


# ---------------------------------------------------------------------------------------------
# 13.5 Потолок реплики меряется по часам — вторая точка проверки
# ---------------------------------------------------------------------------------------------


async def test_pause_before_utterance_end_is_also_over_the_limit(voice_stand: Any) -> None:
    """Вторая точка (diff): звук уложился, а `utterance.end` пришёл после паузы длиннее потолка.

    Падает на реализации, проверяющей потолок только на кадрах звука: там клиент, выдержавший
    паузу, провёл бы реплику мимо ограничения. Байт декодированного аудио тест не считает —
    длительность берётся от часов.
    """
    stand = await voice_stand(VOICE_MODE_UTTERANCE_MAX_SECONDS="0.05")
    uid = await seed_voice_user(stand)

    socket, _ = await stand.session(uid)
    await socket.send_frame({"type": "utterance.begin", "mediaType": "audio/mp4"})
    await socket.send_audio(b"tiny")
    # Пауза выдерживается ПОСЛЕ последнего кадра звука: предмет кейса — вторая точка проверки.
    await asyncio.sleep(0.2)
    await socket.send_frame({"type": "utterance.end"})
    error = await socket.next()

    assert (error["code"], error["scope"]) == ("attachment_too_large", "turn")
    assert stand.transcription.calls == [], "ход не начинается"


# ---------------------------------------------------------------------------------------------
# 13.6 Idle-таймаут не отсчитывается, пока идёт ход
# ---------------------------------------------------------------------------------------------


async def test_idle_timeout_is_suspended_while_a_turn_is_running(voice_stand: Any) -> None:
    """Ход дольше idle-таймаута сокет НЕ закрывает (diff, обе стороны).

    Обратная сторона — кейс `4408` в `test_voice_mode_metrics_adr104.py`: молчание той же
    длительности БЕЗ идущего хода сокет закрывает. Падает на реализации, закрывающей работающий
    сокет: там долгий ответ выглядел бы разрывом сети.
    """
    stand = await voice_stand(VOICE_MODE_IDLE_TIMEOUT_SECONDS="0.05")
    uid = await seed_voice_user(stand, balance=100)

    async def _slow() -> None:
        # Генерация длится заведомо дольше таймаута — кадров ОТ КЛИЕНТА в это время не ждут.
        await asyncio.sleep(0.3)

    stand.script(FIVE_SENTENCES[0], _slow, *FIVE_SENTENCES[1:])

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    assert first_of(frames, "done")["response"]["status"] == "assistant_message"
    assert socket.close_code is None, "работающий сокет не закрывается по idle-таймауту"


# ---------------------------------------------------------------------------------------------
# 13.7 Кадры протокола: отброшенный `interrupt`, две строки отказа
# ---------------------------------------------------------------------------------------------


async def test_interrupt_with_an_unknown_turn_id_is_dropped_silently(voice_stand: Any) -> None:
    """`interrupt` с чужим UUID — ни одного кадра в ответ, соединение живо."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script("Ответ после отброшенного interrupt. ")

    socket, _ = await stand.session(uid)
    await socket.send_frame(
        {"type": "interrupt", "turnId": str(uuid.uuid4()), "reason": "barge_in"}
    )
    frames = await socket.turn()

    assert frames_of(frames, "error") == []
    assert frames[0]["type"] == "transcript", "первым кадром идёт расшифровка нового хода"
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


async def test_interrupt_after_done_is_dropped_silently(voice_stand: Any) -> None:
    """`interrupt` ПОСЛЕ `done` того же хода — штатная гонка, ответа нет.

    Падает на реализации, отвечающей `turn_in_progress`: он означает ровно обратное — «ход ещё
    идёт», — и был бы ложной тревогой на ровном месте.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script("Первый ответ. ")
    stand.script("Второй ответ. ")

    socket, _ = await stand.session(uid)
    done = first_of(await socket.turn(), "done")["response"]
    await socket.send_frame(
        {"type": "interrupt", "turnId": done["messageStepId"], "reason": "user_stop"}
    )

    frames = await socket.turn()
    assert frames_of(frames, "error") == []
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"
    # Намерение относилось к ЗАКРЫТОМУ ходу и на следующий не переносится: он обычный.
    assert frames_of(frames, "interrupted") == []
    assert first_of(frames, "done")["response"]["assistantMessage"] == delta_text(frames)


async def test_interrupt_before_the_first_turn_is_dropped_silently(voice_stand: Any) -> None:
    """`interrupt` до первого хода сеанса — прерывать нечего, ответа нет."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script("Ответ первого хода. ")

    socket, _ = await stand.session(uid)
    await socket.send_frame(
        {"type": "interrupt", "turnId": str(uuid.uuid4()), "reason": "user_stop"}
    )
    frames = await socket.turn()

    assert frames_of(frames, "error") == []
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


@pytest.mark.parametrize(
    ("frame", "raw", "expected_scope"),
    [
        (None, "{not json", "session"),
        ({"type": "totally.unknown"}, None, "session"),
        ({"type": "utterance.end", "generationMode": 42}, None, "turn"),
    ],
)
async def test_invalid_frames_name_their_scope_and_keep_the_socket_alive(
    voice_stand: Any, frame: dict[str, Any] | None, raw: str | None, expected_scope: str
) -> None:
    """Невалидный кадр: `validation_error`, `scope` ВЫЧИСЛЯЕТСЯ, соединение живо.

    `scope` равен тому, к чему относится сам кадр: протокол или сеанс → `session`; ход или
    реплика → `turn`. Падает, если `scope` у всех одинаков или если невалидный кадр закрывает
    сокет.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket, _ = await stand.session(uid)
    if raw is not None:
        await socket._to_app.put({"type": "websocket.receive", "text": raw})  # noqa: SLF001
    else:
        assert frame is not None
        await socket.send_frame(frame)
    error = await socket.next()

    assert error["type"] == "error"
    assert error["code"] == "validation_error"
    assert error["scope"] == expected_scope
    assert socket.close_code is None

    stand.script("Ответ после невалидного кадра. ")
    later = await socket.turn()
    assert first_of(later, "done")["response"]["status"] == "assistant_message"


async def test_any_frame_before_start_is_a_session_scoped_validation_error(
    voice_stand: Any,
) -> None:
    """Любой кадр ДО `start` — `validation_error` со `scope:"session"`, соединение живо."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket = await stand.connect(uid)
    await socket.send_frame({"type": "utterance.begin", "mediaType": "audio/mp4"})
    error = await socket.next()

    assert (error["code"], error["scope"]) == ("validation_error", "session")
    assert socket.close_code is None
    ready = await socket.start()
    assert ready["type"] == "ready"


async def test_media_type_in_the_allowlist_but_not_audio_is_a_turn_refusal(
    voice_stand: Any,
) -> None:
    """Кейс (1): тип ЕСТЬ в общем allowlist вложений, но он не класса `audio`.

    Инвариант — «класс `audio` подмножество общего allowlist, и „не картинка“ недостаточно»:
    отдельная проверка класса обязана быть, иначе `image/png` открыл бы реплику. Отказ —
    `unsupported_media_type` со `scope:"turn"`, реплика не открывается, соединение живо.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket, _ = await stand.session(uid)
    await socket.send_frame({"type": "utterance.begin", "mediaType": "image/png"})
    error = await socket.next()

    assert (error["code"], error["scope"]) == ("unsupported_media_type", "turn")
    assert socket.close_code is None
    # Реплика не открылась: следующий бинарный кадр всё ещё «вне сегмента».
    await socket.send_audio(b"stray")
    stray = await socket.next()
    assert (stray["code"], stray["scope"]) == ("unexpected_binary_frame", "session")


async def test_media_type_outside_the_allowlist_is_rejected_by_the_frame_schema(
    voice_stand: Any,
) -> None:
    """Кейс (2): типа НЕТ в allowlist вложений вовсе — отвергает СХЕМА кадра.

    Граница между двумя кейсами проходит не по имени типа, а по принадлежности общему allowlist:
    отсюда другой код и другая область — `validation_error` со `scope:"session"`.

    ⚠️ Формулировка «выходной тип на вход не проходит» была бы НЕВЕРНА: наборы пересекаются —
    `audio/mpeg` принадлежит обоим (вход — класс `audio` вложений,
    `src/app/chat/attachments.py:68-70`;
    выход — таблица форматов синтеза `src/app/config.py:25-28`). Единственный только-выходной тип
    `audio/aac` в allowlist вложений отсутствует и потому попадает в ЭТОТ кейс, а не в первый.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)

    socket, _ = await stand.session(uid)
    await socket.send_frame({"type": "utterance.begin", "mediaType": "audio/aac"})
    error = await socket.next()

    assert (error["code"], error["scope"]) == ("validation_error", "session")
    assert socket.close_code is None

    # Обратная сторона границы: `audio/mpeg` принадлежит ОБОИМ наборам и на вход проходит.
    stand.transcription.transcripts.append("реплика в mp3")
    stand.script("Ответ на mp3-реплику. ")
    frames = await socket.turn(media_type="audio/mpeg")
    assert first_of(frames, "transcript")["text"] == "реплика в mp3"
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


# ---------------------------------------------------------------------------------------------
# 13.9 Ось режима переспрашивается на каждом ходу
# ---------------------------------------------------------------------------------------------


def _voice_input_overlay(*, enabled: bool) -> None:
    install_snapshot(
        InstanceConfigSnapshot(
            settings={
                SETTING_CHAT_VOICE_INPUT_ENABLED: SettingOverlay(
                    setting_id=SETTING_CHAT_VOICE_INPUT_ENABLED,
                    value=enabled,
                    updated_at=datetime.datetime.now(tz=datetime.UTC),
                )
            }
        )
    )


async def test_axis_removed_through_the_overlay_ends_the_session(voice_stand: Any) -> None:
    """Ось снята ОВЕРЛЕЙОМ посреди сеанса → `voice_mode_disabled` + close `1000` (несущий diff).

    Оверлей — тот путь, который меняется на лету; проверка только в рукопожатии оставила бы
    открытый сокет обслуживать ходы после снятия флага, то есть обещание §8 «немедленно» было бы
    ложным ровно для тех сеансов, ради которых оператор флаг и снимает.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script("Ответ до снятия оси. ")

    socket, _ = await stand.session(uid)
    first = await socket.turn()
    assert first_of(first, "done")["response"]["status"] == "assistant_message"
    balance_before = await balance_of(stand, uid)
    calls_before = len(stand.llm.calls)

    try:
        _voice_input_overlay(enabled=False)
        await socket.begin_utterance()
        error = await socket.next()
        closed = await socket.next()
    finally:
        reset_snapshot()

    assert (error["code"], error["scope"]) == ("voice_mode_disabled", "session")
    assert closed["type"] == "__close__"
    assert closed["code"] == 1000, "шестого close-кода под это событие не заводится"
    assert len(stand.llm.calls) == calls_before, "ход не начат"
    assert await balance_of(stand, uid) == balance_before, "деньги не тронуты"


async def test_axis_left_on_lets_the_second_turn_through(voice_stand: Any) -> None:
    """Против переоценки: ось не снята → второй ход проходит, никакого `error` нет."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script("Первый ответ. ")
    stand.script("Второй ответ. ")

    socket, _ = await stand.session(uid)
    await socket.turn()
    _voice_input_overlay(enabled=True)
    try:
        second = await socket.turn()
    finally:
        reset_snapshot()

    assert frames_of(second, "error") == []
    assert first_of(second, "done")["response"]["status"] == "assistant_message"
    assert socket.close_code is None


# ---------------------------------------------------------------------------------------------
# 13.10 Колбэки хода: `on_turn_start` вместо `on_transcript`
# ---------------------------------------------------------------------------------------------


async def test_transcript_precedes_the_first_delta_and_carries_the_turn_id(
    voice_stand: Any,
) -> None:
    """`turnId` известен ДО обращения к модели и выпущен ОРКЕСТРАТОРОМ (diff).

    Падает на реализации, где транспорт выдумывает собственный идентификатор хода: там
    `transcript.turnId ≠ done.response.messageStepId`.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    types = [f["type"] for f in frames]
    assert types.index("transcript") < types.index("delta")
    assert (
        first_of(frames, "transcript")["turnId"]
        == (first_of(frames, "done")["response"]["messageStepId"])
    )


async def test_transcription_happens_in_the_transport_not_the_orchestrator(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Распознаёт ТРАНСПОРТ, а не оркестратор (diff по слою).

    Фейковый распознаватель зовётся из пути сокета, вложений класса `audio` не создано ни одного,
    а колбэк `on_transcript` оркестратора не вызван ни разу. Падает, если голосовой путь научили
    ходить через предшаг распознавания оркестратора.
    """
    from app.chat.orchestrator import ChatOrchestrator

    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    seen_on_transcript: list[str] = []
    original_run = ChatOrchestrator.run

    async def _spy(self: Any, **kwargs: Any) -> Any:
        if kwargs.get("on_transcript") is not None:
            seen_on_transcript.append("passed")
        return await original_run(self, **kwargs)

    monkeypatch.setattr(ChatOrchestrator, "run", _spy)
    stand.transcription.transcripts.append("распознано транспортом")
    stand.script("Ответ на распознанное. ")

    socket, ready = await stand.session(uid)
    frames = await socket.turn()

    assert len(stand.transcription.calls) == 1
    assert first_of(frames, "transcript")["text"] == "распознано транспортом"
    assert seen_on_transcript == [], "оркестратору колбэк распознавания не передаётся"
    steps = await chat_steps(stand, ready["sessionId"])
    user_steps = [step for step in steps if step["role"] == "user"]
    assert "audio" not in json.dumps(_payload(user_steps[0]), ensure_ascii=False)
