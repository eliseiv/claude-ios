"""Integration: отказы и гейты входа голосового режима (ADR-104 §8–§10).

Норма — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`, разделы
«Integration — отказы (каждая строка таблицы контракта — свой кейс)» и
«Integration — гейты входа».

Каждая строка таблицы отказов `02-api-contracts §Отказы и коды закрытия` получает свой кейс, и
проверяется в нём и КАДР, и судьба ХОДА, и деньги — три величины таблицы, ни одна не выводится из
другой.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests.conftest import auth_headers, make_jwt
from tests.integration.test_voice_mode_billing_adr104 import tts_keys
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
    upstream_failure,
)


def _payload(step: dict[str, Any]) -> dict[str, Any]:
    raw = step["payload"]
    return raw if isinstance(raw, dict) else json.loads(raw)


# ---------------------------------------------------------------------------------------------
# Отказы поставщиков
# ---------------------------------------------------------------------------------------------


async def test_synthesizer_failure_does_not_take_the_turn_away(voice_stand: Any) -> None:
    """Отказ синтезатора ход НЕ отнимает (diff): `scope:"speech"`, `done` полный, сокет жив.

    Деньги за отказ ПОСЛЕ доставленного сегмента — отдельный кейс тарификации; здесь проверяется
    только судьба ХОДА.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.speech.fail_on = 1
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    error = first_of(frames, "error")
    assert (error["code"], error["scope"]) == ("upstream_error", "speech")
    done = first_of(frames, "done")["response"]
    assert done["status"] == "assistant_message"
    assert done["assistantMessage"] == delta_text(frames)
    ledger = await ledger_rows(stand, uid)
    assert done["messageStepId"] in [row["idempotency_key"] for row in ledger], "ход списан"
    assert tts_keys(ledger) == [], "доставлено не было ничего — синтез не оплачен"

    # Соединение живо: следующий ход проходит.
    stand.speech.fail_on = None
    stand.script("Следующий ответ звучит как обычно. ")
    later = await socket.turn()
    assert frames_of(later, "audio.end")


async def test_llm_failure_closes_the_turn_with_the_failure_mark(voice_stand: Any) -> None:
    """Отказ LLM: `scope:"turn"`, ход закрыт пометкой `turnFailed`, ход НЕ списан.

    От отказа синтезатора отличается ТОЛЬКО значением `scope` — обе стороны проверяются
    отдельными кейсами.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(FIVE_SENTENCES[0], upstream_failure())

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    frames = await socket.collect_until("error")

    error = first_of(frames, "error")
    assert (error["code"], error["scope"]) == ("upstream_error", "turn")
    assert error["turnId"] == first_of(frames, "transcript")["turnId"]

    steps = await chat_steps(stand, ready["sessionId"])
    assistant = [step for step in steps if step["role"] == "assistant"]
    assert assistant, "ход обязан закрыться пометкой, а не пропасть"
    assert "turnFailed" in _payload(assistant[-1])
    ledger = await ledger_rows(stand, uid)
    assert error["turnId"] not in [row["idempotency_key"] for row in ledger]
    assert await balance_of(stand, uid) == 100


# ---------------------------------------------------------------------------------------------
# Деньги: ход против синтеза
# ---------------------------------------------------------------------------------------------


async def test_no_credits_for_the_turn_blocks_it_and_never_calls_the_synthesizer(
    voice_stand: Any,
) -> None:
    """Кредитов нет на ход: `done` со `status:"blocked"`, `blockReason:"credits_empty"`."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=0)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    done = first_of(frames, "done")["response"]
    assert done["status"] == "blocked"
    assert done["blockReason"] == "credits_empty"
    assert stand.speech.calls == 0
    assert tts_keys(await ledger_rows(stand, uid)) == []


async def test_no_credits_for_speech_still_answers_in_text(voice_stand: Any) -> None:
    """Кредитов нет на СИНТЕЗ: ответ приходит текстом, `speech.skipped`, ход списан.

    Падает на реализации, роняющей ход целиком: потерять ответ хуже, чем не услышать его.
    Правило `409 insufficient_credits` — принадлежность `POST /v1/chat/speech` и на сокет не
    переносится.
    """
    stand = await voice_stand(TTS_CREDIT_COST="5")
    uid = await seed_voice_user(stand, balance=2)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    skipped = first_of(frames, "speech.skipped")
    assert skipped["reason"] == "insufficient_credits"
    assert stand.speech.calls == 0
    assert frames_of(frames, "audio.end") == []
    done = first_of(frames, "done")["response"]
    assert done["status"] == "assistant_message"
    assert done["assistantMessage"] == delta_text(frames)
    ledger = await ledger_rows(stand, uid)
    assert done["messageStepId"] in [row["idempotency_key"] for row in ledger]
    assert tts_keys(ledger) == []


async def test_nothing_to_speak_keeps_the_turn_successful(voice_stand: Any) -> None:
    """Нечего произносить: ответ целиком из блока кода → `speech.skipped nothing_to_speak`.

    Ход при этом УСПЕШЕН. Падает, если код `422 nothing_to_speak` РУЧКИ синтеза перенесли на ход:
    ответ, состоящий из кода, ронял бы диалог.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script("```python\nprint(1)\nprint(2)\n```")

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    assert first_of(frames, "speech.skipped")["reason"] == "nothing_to_speak"
    assert stand.speech.calls == 0
    done = first_of(frames, "done")["response"]
    assert done["status"] == "assistant_message"
    assert tts_keys(await ledger_rows(stand, uid)) == []


# ---------------------------------------------------------------------------------------------
# Отказы реплики
# ---------------------------------------------------------------------------------------------


async def test_utterance_over_the_byte_limit_is_rejected(voice_stand: Any) -> None:
    """Реплика больше `ATTACHMENT_MAX_BYTES_AUDIO` → `attachment_too_large`, ход не начат."""
    stand = await voice_stand(ATTACHMENT_MAX_BYTES_AUDIO="64")
    uid = await seed_voice_user(stand)

    socket, _ = await stand.session(uid)
    await socket.send_frame({"type": "utterance.begin", "mediaType": "audio/mp4"})
    await socket.send_audio(b"x" * 128)
    error = await socket.next()

    assert (error["code"], error["scope"]) == ("attachment_too_large", "turn")
    assert stand.transcription.calls == []
    # Ход не начинается: закрывающий кадр отвергнутой реплики молча её хоронит.
    await socket.send_frame({"type": "utterance.end"})
    assert stand.llm.calls == []
    # Соединение живо и следующая, нормальная реплика проходит.
    stand.script("Ответ после отказа. ")
    later = await socket.turn(audio=b"tiny")
    assert first_of(later, "done")["response"]["status"] == "assistant_message"


async def test_utterance_over_the_duration_limit_is_rejected(voice_stand: Any) -> None:
    """Реплика длиннее `VOICE_MODE_UTTERANCE_MAX_SECONDS` → тот же отказ, ход не начат.

    Два независимых предела, поэтому два кейса: удаление любой половины проверки не уронило бы
    соседнюю (на сильно сжатом кодеке одно из другого не выводится).

    Интервал здесь ВЫДЕРЖИВАЕТСЯ намеренно: предмет кейса — потолок по ЧАСАМ, и «прошедшее
    время» и есть проверяемая величина, а не способ синхронизации (как и в кейсе idle-таймаута).
    Потолок 20 мс против выдержки 100 мс с запасом перекрывает разрешение системных часов.
    """
    stand = await voice_stand(VOICE_MODE_UTTERANCE_MAX_SECONDS="0.02")
    uid = await seed_voice_user(stand)

    socket, _ = await stand.session(uid)
    await socket.send_frame({"type": "utterance.begin", "mediaType": "audio/mp4"})
    await asyncio.sleep(0.1)
    await socket.send_audio(b"tiny")
    error = await socket.next()

    assert (error["code"], error["scope"]) == ("attachment_too_large", "turn")
    assert stand.transcription.calls == []


async def test_silence_is_a_predicate_refusal_not_a_validation_error(voice_stand: Any) -> None:
    """Пустая реплика (тишина) → `empty_audio`, ход не начат.

    Именно `empty_audio`, а не `validation_error`: правило HTTP-пути на сокет не переносится, и
    приложение обязано отличать тишину от кривого запроса.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.transcription.transcripts.append("")

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    frames = await socket.collect_until("error")

    error = first_of(frames, "error")
    assert (error["code"], error["scope"]) == ("empty_audio", "turn")
    assert stand.llm.calls == []


async def test_binary_frame_outside_an_utterance_keeps_the_socket_alive(voice_stand: Any) -> None:
    """Бинарный кадр вне открытого сегмента: `unexpected_binary_frame`, соединение ЖИВО.

    Падает на реализации, закрывающей сокет на безобидной гонке.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)

    socket, _ = await stand.session(uid)
    await socket.send_audio(b"stray-bytes")
    error = await socket.next()

    assert (error["code"], error["scope"]) == ("unexpected_binary_frame", "session")
    stand.script("Ответ после отброшенного кадра. ")
    frames = await socket.turn()
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


async def test_socket_drop_does_not_cancel_the_turn(voice_stand: Any) -> None:
    """Обрыв сокета ход НЕ отменяет (diff): шаг персистится и ход тарифицируется.

    Падает на реализации, отменяющей ход по разрыву транспорта: там реплика осталась бы без
    ответа, а отмена пришла бы `CancelledError`-ом мимо ветки закрытия хода.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)

    socket, ready = await stand.session(uid)
    await socket.begin_utterance()
    await socket.disconnect()
    await socket.aclose()

    steps = await chat_steps(stand, ready["sessionId"])
    assistant = [step for step in steps if step["role"] == "assistant"]
    assert len(assistant) == 1, "ход доходит до конца и персистится"
    assert "turnFailed" not in _payload(assistant[0])
    ledger = await ledger_rows(stand, uid)
    assert [row["idempotency_key"] for row in ledger], "ход тарифицирован"


# ---------------------------------------------------------------------------------------------
# Гейты входа: ось режима и ключ
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    ["VOICE_MODE_ENABLED", "VOICE_INPUT_ENABLED", "VOICE_OUTPUT_ENABLED"],
)
async def test_each_half_of_the_axis_denies_the_upgrade(voice_stand: Any, flag: str) -> None:
    """Ось выключена — ТРИ кейса, а не один: ось составная, и забыть половину проще всего.

    Все три → `422 voice_mode_disabled` ДО апгрейда, в едином конверте ошибки.
    """
    stand = await voice_stand(**{flag: "false"})
    uid = await seed_voice_user(stand)

    with pytest.raises(VoiceHandshakeDenied) as denied:
        await stand.connect(uid)

    assert denied.value.status == 422
    assert denied.value.payload["error"]["code"] == "voice_mode_disabled"
    assert "requestId" in denied.value.payload["error"]


async def test_all_three_flags_on_upgrade_the_socket(voice_stand: Any) -> None:
    """Обратная сторона: все три включены → апгрейд выполнен."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)

    socket = await stand.connect(uid)
    assert socket.accepted is True


async def test_missing_key_is_not_collapsed_into_the_flag_refusal(voice_stand: Any) -> None:
    """Ключ пуст: режим включён → `503 voice_mode_not_configured`. `422` и `503` не схлопнуты."""
    stand = await voice_stand(OPENAI_API_KEY="")
    uid = await seed_voice_user(stand)

    with pytest.raises(VoiceHandshakeDenied) as denied:
        await stand.connect(uid)

    assert denied.value.status == 503
    assert denied.value.payload["error"]["code"] == "voice_mode_not_configured"


# ---------------------------------------------------------------------------------------------
# Гейты входа: auth и изоляция
# ---------------------------------------------------------------------------------------------


async def test_upgrade_without_a_token_is_denied_before_the_upgrade(voice_stand: Any) -> None:
    """Auth: без JWT → `401` ДО апгрейда, в едином конверте ошибки."""
    stand = await voice_stand()

    with pytest.raises(VoiceHandshakeDenied) as denied:
        await stand.connect(token="")

    assert denied.value.status == 401
    assert denied.value.payload["error"]["code"] == "unauthorized"


async def test_token_in_the_query_string_gives_no_access(voice_stand: Any) -> None:
    """Тот же токен в query-строке доступа НЕ даёт: query уезжает в access-логи прокси.

    Падает, если query-токен принимается.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    token = make_jwt(uid)

    with pytest.raises(VoiceHandshakeDenied) as denied:
        await stand.connect(token="", path=f"/v1/chat/voice?token={token}")

    assert denied.value.status == 401
    assert denied.value.payload["error"]["code"] == "unauthorized"


async def test_foreign_and_missing_sessions_are_indistinguishable(voice_stand: Any) -> None:
    """Изоляция: чужой `sessionId` → `session_not_found` + close `4400`; своя открывается.

    Чужая и несуществующая сессия дают ОДИН И ТОТ ЖЕ код и текст: существование чужой не
    раскрывается.
    """
    import uuid as _uuid

    stand = await voice_stand()
    owner = await seed_voice_user(stand)
    stranger = await seed_voice_user(stand)

    stand.script("Ответ владельца сессии. ")
    owner_socket, ready = await stand.session(owner)
    await owner_socket.turn()

    foreign = await stand.connect(stranger)
    foreign_error = await foreign.start(sessionId=ready["sessionId"])
    foreign_close = await foreign.next()

    absent = await stand.connect(stranger)
    absent_error = await absent.start(sessionId=str(_uuid.uuid4()))
    absent_close = await absent.next()

    assert foreign_error["code"] == "session_not_found"
    assert foreign_error["scope"] == "session"
    assert foreign_close["code"] == 4400
    assert (foreign_error["code"], foreign_error["message"]) == (
        absent_error["code"],
        absent_error["message"],
    )
    assert absent_close["code"] == 4400

    # Своя сессия открывается нормально.
    own = await stand.connect(owner)
    own_ready = await own.start(sessionId=ready["sessionId"])
    assert own_ready["type"] == "ready"


# ---------------------------------------------------------------------------------------------
# Гейты входа: терминальность отказов
# ---------------------------------------------------------------------------------------------


async def test_code_assistant_mode_is_terminal(voice_stand: Any) -> None:
    """`assistantMode="code"` на `start` → `scope:"session"` + close `4400`, сокет закрыт."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)

    socket = await stand.connect(uid)
    error = await socket.start(assistantMode="code")
    closed = await socket.next()

    assert error["code"] == "unsupported_assistant_mode"
    assert error["scope"] == "session"
    assert closed["code"] == 4400


async def test_study_learn_generation_mode_is_per_turn(voice_stand: Any) -> None:
    """`generationMode="study_learn"` на кадре хода → `scope:"turn"`, сокет ЖИВ.

    Обратная сторона той же пары: следующий ход в `general` проходит нормально. Падает на
    реализации, схлопнувшей две строки таблицы в одну.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)

    socket, _ = await stand.session(uid)
    await socket.begin_utterance(generation_mode="study_learn")
    frames = await socket.collect_until("error")

    error = first_of(frames, "error")
    assert error["code"] == "unsupported_generation_mode"
    assert error["scope"] == "turn"
    assert stand.llm.calls == [], "ход не начинается"

    stand.script("Обычный ответ в general. ")
    later = await socket.turn(generation_mode="general")
    assert first_of(later, "done")["response"]["status"] == "assistant_message"


async def test_chat_assistant_mode_and_allowed_generation_modes_pass(voice_stand: Any) -> None:
    """Обратная сторона явных отказов: `assistantMode="chat"` и `general` проходят.

    Падает при молчаливой деградации: отказ обязан быть явным, а разрешённый вход — работать.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script("Ответ в режиме chat. ")

    socket, ready = await stand.session(uid, assistantMode="chat")
    assert ready["type"] == "ready"
    frames = await socket.turn(generation_mode="general")
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


async def test_turn_rate_limit_inside_the_session_keeps_the_socket_open(voice_stand: Any) -> None:
    """Отказ лимита ходов на поднятом сокете → `rate_limited` `scope:"turn"`, close-кадра НЕТ.

    Падает на реализации, закрывающей сокет кодом `4429`: такого close-кода не существует.
    """
    from app.api_gateway.routers import chat_voice

    stand = await voice_stand()
    uid = await seed_voice_user(stand)

    socket, _ = await stand.session(uid)

    async def _deny(**_kwargs: Any) -> bool:
        return False

    original = chat_voice.enforce_chat_limits
    chat_voice.enforce_chat_limits = _deny  # type: ignore[assignment]
    try:
        await socket.begin_utterance()
        frames = await socket.collect_until("error")
        error = first_of(frames, "error")
        assert (error["code"], error["scope"]) == ("rate_limited", "turn")
        assert socket.close_code is None, "соединение обязано остаться живым"
    finally:
        chat_voice.enforce_chat_limits = original  # type: ignore[assignment]

    stand.script("Ответ после снятия лимита. ")
    later = await socket.turn()
    assert first_of(later, "done")["response"]["status"] == "assistant_message"


# ---------------------------------------------------------------------------------------------
# Гейты входа: режим сеанса и каталог
# ---------------------------------------------------------------------------------------------


async def test_byok_session_runs_on_the_user_key_and_pays_speech_internally(
    voice_stand: Any,
) -> None:
    """`mode:"byok"` (обе стороны): ход идёт ключом пользователя, синтез — внутренними кредитами.

    Падает на реализации, жёстко фиксирующей `credits`.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100, byok_enabled=True, byok_status="valid")
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid, mode="byok")
    frames = await socket.turn()

    done = first_of(frames, "done")["response"]
    assert stand.llm.calls[-1]["api_key"] == "sk-ant-user-key"
    ledger = await ledger_rows(stand, uid)
    keys = [row["idempotency_key"] for row in ledger]
    assert done["messageStepId"] not in keys, "ход ключом пользователя внутренних кредитов не берёт"
    assert tts_keys(ledger) == [f"tts:{done['stepId']}:default_female"]


async def test_credits_session_is_the_default(voice_stand: Any) -> None:
    """Обратная сторона: `start` без `mode` создаёт сессию с `credits` — ход списывается."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=100)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    done = first_of(await socket.turn(), "done")["response"]

    keys = [row["idempotency_key"] for row in await ledger_rows(stand, uid)]
    assert done["messageStepId"] in keys


async def test_byok_with_zero_balance_answers_without_sound(voice_stand: Any) -> None:
    """BYOK с нулевым балансом (diff): ход идёт, звука нет, `speech.skipped`.

    Падает на реализации, роняющей ход.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand, balance=0, byok_enabled=True, byok_status="valid")
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid, mode="byok")
    frames = await socket.turn()

    assert first_of(frames, "speech.skipped")["reason"] == "insufficient_credits"
    assert frames_of(frames, "audio.end") == []
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"
    assert stand.speech.calls == 0


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        (None, True),
        ("VOICE_MODE_ENABLED", False),
        ("VOICE_INPUT_ENABLED", False),
        ("VOICE_OUTPUT_ENABLED", False),
    ],
)
async def test_voice_mode_enabled_in_the_catalog(
    voice_stand: Any, flag: str | None, expected: bool
) -> None:
    """`voiceModeEnabled` в каталоге (diff, обе стороны): `true` ровно при всех трёх флагах.

    Поле присутствует ВСЕГДА, в том числе при `enabled: false`: иначе старые клиенты не отличили
    бы «не умеет» от «бэкенд старее».
    """
    stand = await voice_stand(**({} if flag is None else {flag: "false"}))
    uid = await seed_voice_user(stand)

    catalog = await stand.http.get("/v1/voices", headers=auth_headers(uid))
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    assert "voiceModeEnabled" in body
    assert body["voiceModeEnabled"] is expected
    if flag == "VOICE_OUTPUT_ENABLED":
        # Ось каталога отдельная от оси режима: озвучка выключена — список пуст, но поле есть.
        assert body["enabled"] is False
        assert body["voices"] == []
