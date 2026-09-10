"""Integration: сквозной путь голосового сеанса на РАБОЧЕМ сокете (ADR-104).

Норма — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`, разделы
«Integration — сквозной путь сеанса» и «Integration — инструменты».

Сегменты рождаются из фактических дельт фейкового LLM-клиента (`stream_chunks`), а не подаются
в сегментатор готовым списком: цепь «сокет → оркестратор → провайдер → сегментатор → кадр»
проверяется целиком.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import text as sql_text

from tests.conftest import auth_headers, seed_user
from tests.voice_harness import (
    FIVE_SENTENCES,
    delta_text,
    first_of,
    frames_of,
)


async def seed_voice_user(stand: Any, *, balance: int = 100, **kwargs: Any) -> uuid.UUID:
    async with stand.sessionmaker() as session:
        return await seed_user(session, balance=balance, subscription="active", **kwargs)


async def chat_steps(stand: Any, session_id: str) -> list[dict[str, Any]]:
    async with stand.sessionmaker() as session:
        rows = await session.execute(
            sql_text(
                "SELECT id, role, payload, message_step_id FROM chat_steps "
                "WHERE session_id = :sid ORDER BY seq"
            ),
            {"sid": session_id},
        )
        return [dict(row._mapping) for row in rows]  # noqa: SLF001 — чтение сырых строк


async def ledger_rows(stand: Any, user_id: uuid.UUID) -> list[dict[str, Any]]:
    async with stand.sessionmaker() as session:
        rows = await session.execute(
            sql_text(
                "SELECT idempotency_key, amount, type FROM ledger_transactions "
                "WHERE user_id = :uid ORDER BY id"
            ),
            {"uid": str(user_id)},
        )
        return [dict(row._mapping) for row in rows]  # noqa: SLF001


async def balance_of(stand: Any, user_id: uuid.UUID) -> int:
    async with stand.sessionmaker() as session:
        value = await session.scalar(
            sql_text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(user_id)}
        )
        return int(value or 0)


def gate() -> tuple[asyncio.Event, Any]:
    """Барьер в потоке дельт: событие + вызываемый элемент для `stand.script`."""
    event = asyncio.Event()

    async def _wait() -> None:
        await event.wait()

    return event, _wait


# ---------------------------------------------------------------------------------------------
# Сквозной путь сеанса
# ---------------------------------------------------------------------------------------------


async def test_turn_goes_through_end_to_end(voice_stand: Any) -> None:
    """Ход доходит целиком (diff достижимости): расшифровка, дельты, звук, `done`.

    Фейковый распознаватель вызван один раз, фейковый синтезатор — по разу на сегмент.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.transcription.transcripts.append("расскажи что-нибудь")
    stand.script(*FIVE_SENTENCES)

    socket, ready = await stand.session(uid)
    assert ready["voiceId"] == "default_female"
    frames = await socket.turn()

    assert first_of(frames, "transcript")["text"] == "расскажи что-нибудь"
    assert len(frames_of(frames, "delta")) == len(FIVE_SENTENCES)
    assert len(frames_of(frames, "audio.begin")) == len(FIVE_SENTENCES)
    assert len(frames_of(frames, "audio.end")) == len(FIVE_SENTENCES)
    assert all(frame["data"] for frame in frames_of(frames, "__bytes__"))
    assert len(stand.transcription.calls) == 1
    assert stand.speech.calls == len(FIVE_SENTENCES)
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


async def test_done_carries_the_same_chat_response_as_the_http_turn(voice_stand: Any) -> None:
    """`done.response` — тот же `ChatResponse` (регрессия против ВТОРОГО формата ответа).

    Множество ключей сравнивается с эталоном `POST /v1/chat/v2/run` на эквивалентном ходе.
    Падает, если у голосового канала завелась своя форма ответа.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script("Ответ ассистента целиком. ")

    socket, _ = await stand.session(uid)
    frames = await socket.turn()
    voice_keys = set(first_of(frames, "done")["response"])

    stand.script("Ответ ассистента целиком. ")
    http = await stand.http.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "то же самое", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert http.status_code == 200, http.text
    assert voice_keys == set(http.json())


async def test_turn_id_links_every_frame_to_the_turn(voice_stand: Any) -> None:
    """`turnId` каждого кадра равен `done.response.messageStepId` того же хода.

    Падает на реализации с собственным пространством идентификаторов: связывать звук с ходом
    «по порядку» значило бы полагаться на порядок там, где есть точный ключ.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    turn_id = first_of(frames, "done")["response"]["messageStepId"]
    assert turn_id
    carried = [f for f in frames if f["type"] not in ("__bytes__", "__close__")]
    assert {f["turnId"] for f in carried} == {turn_id}


async def test_voice_turn_is_indistinguishable_from_a_typed_one(voice_stand: Any) -> None:
    """Голосовой ход неотличим от набранного (diff): user-шаг — обычный текст расшифровки.

    Падает, если завели признак «сказано голосом» или вложение класса `audio`.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.transcription.transcripts.append("это сказано голосом")
    stand.script("Понял тебя целиком. ")

    socket, ready = await stand.session(uid)
    await socket.turn()

    history = await stand.http.get(f"/v1/chats/{ready['sessionId']}", headers=auth_headers(uid))
    assert history.status_code == 200, history.text
    user_steps = [step for step in history.json()["steps"] if step["role"] == "user"]
    assert len(user_steps) == 1
    serialized = str(user_steps[0])
    assert "это сказано голосом" in serialized
    for marker in ("audio", "voice", "spokenBy", "transcribed"):
        assert marker not in serialized


async def test_second_turn_continues_the_same_session(voice_stand: Any) -> None:
    """Второй ход того же соединения переиспользует `sessionId` и продолжает историю."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script("Первый ответ ассистента. ")
    stand.script("Второй ответ ассистента. ")

    socket, ready = await stand.session(uid)
    first = await socket.turn()
    second = await socket.turn()

    assert first_of(first, "done")["response"]["sessionId"] == ready["sessionId"]
    assert first_of(second, "done")["response"]["sessionId"] == ready["sessionId"]
    steps = await chat_steps(stand, ready["sessionId"])
    assert len([step for step in steps if step["role"] == "user"]) == 2


async def test_start_is_accepted_once_per_connection(voice_stand: Any) -> None:
    """Session-fixed поля повторно не принимаются: второй `start` — отказ, сеанс не меняется."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)

    socket, ready = await stand.session(uid)
    again = await socket.start(model="gpt-4o")

    assert again["type"] == "error"
    assert again["code"] == "validation_error"
    assert again["scope"] == "session"
    stand.script("Ответ после повторного start. ")
    frames = await socket.turn()
    assert first_of(frames, "done")["response"]["sessionId"] == ready["sessionId"]


async def test_parallel_turn_is_rejected_and_the_socket_survives(voice_stand: Any) -> None:
    """`utterance.end` при незакрытом ходе → `turn_in_progress`; соединение живо, звук отброшен."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    event, barrier = gate()
    stand.script(FIVE_SENTENCES[0], barrier, *FIVE_SENTENCES[1:])
    stand.script("Ответ следующего хода. ")

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    assert (await socket.next())["type"] == "transcript"

    # Второй ход при незакрытом первом: задача хода стоит на барьере, значит она заведомо жива.
    await socket.begin_utterance(audio=b"second-utterance")
    rejected: dict[str, Any] | None = None
    while rejected is None:
        frame = await socket.next()
        if frame["type"] == "error":
            rejected = frame
    assert (rejected["code"], rejected["scope"]) == ("turn_in_progress", "turn")

    event.set()
    frames = await socket.collect_until("done")
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"
    # Соединение живо: следующий ход проходит нормально.
    later = await socket.turn()
    assert first_of(later, "done")["response"]["status"] == "assistant_message"


async def test_text_frame_runs_the_same_turn_without_transcription(voice_stand: Any) -> None:
    """Набранное сообщение внутри голосового сеанса — тот же ход, просто без распознавания."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script("Ответ на набранное сообщение. ")

    socket, _ = await stand.session(uid)
    await socket.send_frame({"type": "text", "text": "набрано руками"})
    frames = await socket.collect_until("done")

    assert frames_of(frames, "transcript") == []
    assert stand.transcription.calls == []
    assert frames_of(frames, "audio.end")
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"


# ---------------------------------------------------------------------------------------------
# Совокупный потолок на рабочем сокете (обратная сторона unit-кейса)
# ---------------------------------------------------------------------------------------------


async def test_cap_truncates_speech_but_not_the_answer(voice_stand: Any) -> None:
    """Потолок режет РЕЧЬ, а не ответ: `truncated: true`, а текст `delta`/`done` полный.

    Падает, если потолок перенесли на генерацию, и падает на посегментном применении: там
    `truncated` не выставился бы ни разу.
    """
    stand = await voice_stand(TTS_MAX_CHARS="80")
    uid = await seed_voice_user(stand)
    stand.script(*FIVE_SENTENCES)

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    ends = frames_of(frames, "audio.end")
    assert ends[-1]["truncated"] is True
    assert len(ends) < len(FIVE_SENTENCES)
    assert sum(len(text) for text in stand.speech.texts) <= 80

    answer = first_of(frames, "done")["response"]["assistantMessage"]
    assert answer == delta_text(frames)
    for sentence in FIVE_SENTENCES:
        assert sentence.strip() in answer


# ---------------------------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------------------------

_TOOL_CALLS = [
    ("calendar.read", {"start": "2026-09-10T00:00:00Z", "end": "2026-09-11T00:00:00Z"}),
    ("reminders.read", {"includeCompleted": False}),
]


async def test_turn_barrier_is_unchanged_on_the_socket(voice_stand: Any) -> None:
    """Барьер хода не изменён (diff): оба вызова в `done`, continuation только после всех.

    Частичный батч → повторный `done` со `status:"tool_call"` и ОСТАВШИМИСЯ вызовами, провайдер
    при этом не вызван.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script_tool_call(_TOOL_CALLS, "Сейчас посмотрю оба источника. ")

    socket, _ = await stand.session(uid)
    frames = await socket.turn()
    done = first_of(frames, "done")["response"]
    assert done["status"] == "tool_call"
    assert len(done["toolCalls"]) == 2
    calls_before = len(stand.llm.calls)

    first_id = done["toolCalls"][0]["id"]
    second_id = done["toolCalls"][1]["id"]
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": done["messageStepId"],
            "results": [{"toolCallId": first_id, "result": {"ok": True}}],
        }
    )
    partial = await socket.collect_until("done")
    partial_done = first_of(partial, "done")["response"]
    assert partial_done["status"] == "tool_call"
    assert [call["id"] for call in partial_done["toolCalls"]] == [second_id]
    assert len(stand.llm.calls) == calls_before, "провайдер не вызывается до закрытия барьера"

    stand.script("Оба источника посмотрел, отвечаю. ")
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": done["messageStepId"],
            "results": [{"toolCallId": second_id, "result": {"ok": True}}],
        }
    )
    final = await socket.collect_until("done")
    assert first_of(final, "done")["response"]["status"] == "assistant_message"


async def test_tool_call_leg_speaks_its_accompanying_text(voice_stand: Any) -> None:
    """Сопутствующий текст ноги `tool_call` озвучивается: пара `audio.begin`/`audio.end` есть."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script_tool_call(_TOOL_CALLS[:1], "Сейчас посмотрю календарь и отвечу. ")

    socket, _ = await stand.session(uid)
    frames = await socket.turn()

    assert frames_of(frames, "audio.begin"), "нога tool_call обязана звучать"
    assert frames_of(frames, "audio.end")
    assert first_of(frames, "done")["response"]["status"] == "tool_call"


async def test_tool_set_is_not_narrowed_by_the_voice_channel(voice_stand: Any) -> None:
    """Набор инструментов не сужен (diff): те же имена, что на эквивалентном HTTP-ходе.

    Падает, если голосовой режим стал шестой осью гейтинга.
    """
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script("Ответ голосового хода. ")

    socket, _ = await stand.session(uid)
    await socket.turn()
    voice_tools = {tool["name"] for tool in stand.llm.calls[-1]["tools"]}

    stand.script("Ответ HTTP-хода. ")
    http = await stand.http.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "то же самое", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert http.status_code == 200, http.text
    http_tools = {tool["name"] for tool in stand.llm.calls[-1]["tools"]}

    assert voice_tools == http_tools


async def test_segment_numbering_and_cap_are_per_turn_across_both_legs(voice_stand: Any) -> None:
    """M1/M2 на РАБОЧЕМ сокете: ход с клиентскими инструментами — две ноги, один `turnId`.

    Совокупный расход `TTS_MAX_CHARS` считается по ходу, а номера сегментов строго монотонны и
    без повторов. На одноногом ходе оба дефекта невидимы по построению: откат `VoiceTurnBudget`
    в объект ноги (мутация M1/M2) удваивает расход и заставляет вторую ногу начать с `segment: 0`.
    """
    stand = await voice_stand(TTS_MAX_CHARS="120")
    uid = await seed_voice_user(stand)
    stand.script_tool_call(_TOOL_CALLS[:1], *FIVE_SENTENCES[:3])

    socket, _ = await stand.session(uid)
    first_leg = await socket.turn()
    done = first_of(first_leg, "done")["response"]
    assert done["status"] == "tool_call"

    stand.script(*FIVE_SENTENCES[3:])
    await socket.send_frame(
        {
            "type": "tool.result",
            "turnId": done["messageStepId"],
            "results": [{"toolCallId": done["toolCalls"][0]["id"], "result": {"ok": True}}],
        }
    )
    second_leg = await socket.collect_until("done")

    assert first_of(second_leg, "done")["response"]["messageStepId"] == done["messageStepId"]
    numbers = [frame["segment"] for frame in frames_of(first_leg + second_leg, "audio.end")]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers)), "пара (turnId, segment) обязана быть уникальной"
    assert sum(len(text) for text in stand.speech.texts) <= 120
