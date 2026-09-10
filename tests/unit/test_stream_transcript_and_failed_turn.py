"""Две правки по отчётам iOS: ранняя расшифровка и пометка оборвавшегося хода.

1. РАСШИФРОВКА ПРИХОДИЛА ТОЛЬКО В КОНЦЕ. Распознавание идёт ВНУТРИ хода, отдельной ручки нет,
   и расшифровка возвращалась лишь в финальном ответе — человек ждал весь ход, чтобы увидеть
   свои же слова. Замер на проде: распознавание около секунды, ход около десяти.

2. УПАВШИЙ ХОД ОСТАВЛЯЛ РЕПЛИКУ БЕЗ ОТВЕТА. Шаг пользователя коммитится ДО сетевого вызова
   намеренно, поэтому откат его не достаёт: на следующем ходу модель отвечала на прошлую
   реплику, а не на новую (прод 2026-09-09, avelyra).

Кейсы разделены по инвариантам; каждый падает при откате СВОЕЙ части.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.api_gateway.routers.chat import _stream_event_frame
from app.chat.orchestrator import (
    ChatOrchestrator,
    ChatRunOut,
    ChatStreamEvent,
    _turn_failed_text,
)

# --------------------------------- событие расшифровки ---------------------------------


def test_transcript_event_carries_the_text() -> None:
    ev = ChatStreamEvent.transcript("привет")
    assert ev.kind == "transcript"
    assert ev.text == "привет"


def test_transcript_frame_is_a_named_sse_event() -> None:
    raw = _stream_event_frame(ChatStreamEvent.transcript("привет")).decode()
    assert raw.startswith("event: transcript\n")
    payload = json.loads(raw.split("data:", 1)[1].strip())
    assert payload == {"text": "привет"}


class _RunStub:
    """Минимальный `self` для `ChatOrchestrator.run`: только то, что этот метод трогает."""

    def __init__(self, transcript: str | None) -> None:
        self._transcript = transcript
        self.turn_started = False

    async def _transcribe_voice(
        self, message: str, attachments: Any, locale: str | None = None
    ) -> tuple[str, Any, str | None]:
        return message, attachments, self._transcript

    async def _run_turn(self, **_: Any) -> ChatRunOut:
        self.turn_started = True
        # Настоящий `ChatRunOut`, а не заглушка: `run` дописывает расшифровку через
        # `dataclasses.replace`, и подделка молча разошлась бы с продовым путём.
        return ChatRunOut(status="assistant_message", session_id=uuid.uuid4())


async def _run(stub: _RunStub, seen: list[str]) -> Any:
    async def on_transcript(text: str) -> None:
        seen.append(text)
        assert not stub.turn_started, "расшифровка обязана уйти ДО начала хода"

    return await ChatOrchestrator.run(
        stub,  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        project_id=None,
        session_id=None,
        message="hi",
        mode="credits",
        on_transcript=on_transcript,
    )


@pytest.mark.asyncio
async def test_transcript_is_emitted_before_the_turn_starts() -> None:
    """Смысл правки: слова человека видны ДО обращения к модели, а не после ответа."""
    stub = _RunStub("привет, это расшифровка")
    seen: list[str] = []
    await _run(stub, seen)
    assert seen == ["привет, это расшифровка"]


@pytest.mark.asyncio
async def test_no_transcript_event_for_a_typed_turn() -> None:
    """Набранный руками ход расшифровки не имеет — пустого события быть не должно."""
    stub = _RunStub(None)
    seen: list[str] = []
    await _run(stub, seen)
    assert seen == []


@pytest.mark.asyncio
async def test_failing_callback_does_not_break_the_turn() -> None:
    """Потерянное событие хуже упавшего ответа только если из-за него падает ответ."""
    stub = _RunStub("текст")

    async def broken(_: str) -> None:
        raise RuntimeError("клиент отвалился")

    out = await ChatOrchestrator.run(
        stub,  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        project_id=None,
        session_id=None,
        message="hi",
        mode="credits",
        on_transcript=broken,
    )
    assert out is not None
    assert stub.turn_started


# ------------------------------ пометка оборвавшегося хода ------------------------------


def test_marker_text_follows_the_client_locale() -> None:
    assert "не получен" in _turn_failed_text("ru-RU")
    assert "not received" in _turn_failed_text("de")  # неизвестный язык → английский


class _FakeRepo:
    def __init__(self, *, has_assistant: bool) -> None:
        self._has_assistant = has_assistant
        self.added: list[dict[str, Any]] = []

    async def has_terminal_assistant_step(self, _s: uuid.UUID, _m: uuid.UUID) -> bool:
        # ADR-104 §13.1: предикат переименован (было `has_assistant_step` — «есть хоть один
        # шаг», стало «есть ЗАВЕРШАЮЩИЙ шаг»). Утверждение теста НЕ меняется: он по-прежнему
        # проверяет, что при уже отвеченном ходе пометка не пишется, а при неотвеченном —
        # пишется. Сменился только СПОСОБ, которым страж адресует величину.
        return self._has_assistant

    async def add_step(self, **kwargs: Any) -> Any:
        self.added.append(kwargs)
        return object()


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _MarkStub:
    def __init__(self, repo: _FakeRepo, session: _FakeSession) -> None:
        class _Deps:
            pass

        self._deps = _Deps()
        self._deps.repo = repo  # type: ignore[attr-defined]
        self._session = session


async def _mark(stub: _MarkStub, locale: str = "ru") -> None:
    await ChatOrchestrator._mark_turn_failed(
        stub,  # type: ignore[arg-type]
        session_id=uuid.uuid4(),
        message_step_id=uuid.uuid4(),
        locale=locale,
        reason="UpstreamError",
    )


@pytest.mark.asyncio
async def test_marker_closes_a_turn_that_never_answered() -> None:
    repo, session = _FakeRepo(has_assistant=False), _FakeSession()
    await _mark(_MarkStub(repo, session))
    assert len(repo.added) == 1
    step = repo.added[0]
    assert step["role"] == "assistant"
    assert step["payload"]["content"][0]["type"] == "text"
    assert "не получен" in step["payload"]["content"][0]["text"]
    assert step["payload"]["turnFailed"]["reason"] == "UpstreamError"
    assert session.commits == 1, "без собственного commit пометка исчезнет вместе с откатом"


@pytest.mark.asyncio
async def test_marker_is_not_written_when_the_turn_already_answered() -> None:
    """Существующий шаг ассистента означает, что ход что-то ответил; вторая пометка — ложь."""
    repo, session = _FakeRepo(has_assistant=True), _FakeSession()
    await _mark(_MarkStub(repo, session))
    assert repo.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_marker_failure_is_swallowed_and_never_masks_the_real_error() -> None:
    repo, session = _FakeRepo(has_assistant=False), _FakeSession()

    async def boom(**_: Any) -> Any:
        raise RuntimeError("база недоступна")

    repo.add_step = boom  # type: ignore[assignment]
    await _mark(_MarkStub(repo, session))  # не должно бросить
