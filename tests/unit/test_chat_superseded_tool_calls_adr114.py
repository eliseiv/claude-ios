"""Брошенный вызов инструмента и новое сообщение пользователя (ADR-114).

Нормативный перечень — `docs/modules/chat-orchestrator/09-testing.md`
§«Брошенный клиентский вызов и новое сообщение (ADR-114)». Здесь — кейсы, выразимые без БД:

- сборка нейтральной истории `_neutral_history_from_steps` (§1–§2) и её перевод в провайдерский
  ввод обоих провайдеров (Anthropic Messages и OpenAI Responses) с ассертом ВАЛИДНОСТИ ввода:
  у каждого вызова ровно один результат, и стоит он там, где провайдер его ждёт;
- проверки 1 и 2 `ChatOrchestrator.tool_result` (§3) на подменённом репозитории;
- `409` в OpenAPI обоих `/tool-result`.

Кейсы, которым нужна реальная БД (откат записи проверки 2, `tool_calls.status` после хода, баланс,
«отравленный» чат через HTTP), — зона интеграционных тестов.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from app.chat.anthropic_client import AnthropicClient
from app.chat.llm_client import NeutralMessage
from app.chat.openai_responses_client import OpenAIResponsesClient
from app.chat.orchestrator import (
    ChatOrchestrator,
    ToolResultIn,
    _neutral_history_from_steps,
)
from app.errors import ConflictError

_MISSING = "tool_result_missing"


# --------------------------------------------------------------------------- fixtures: steps


@dataclass
class _Step:
    role: str
    payload: dict[str, Any]
    message_step_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def _user(text: str, turn: uuid.UUID | None = None) -> _Step:
    return _Step("user", {"content": [{"type": "text", "text": text}]}, turn or uuid.uuid4())


def _assistant(turn: uuid.UUID, *calls: tuple[str, str], text: str | None = None) -> _Step:
    blocks: list[dict[str, Any]] = []
    if text is not None:
        blocks.append({"type": "text", "text": text})
    for call_id, name in calls:
        blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": {"q": "x"}})
    return _Step("assistant", {"content": blocks}, turn)


def _tool(turn: uuid.UUID, call_id: str, name: str, result: dict[str, Any]) -> _Step:
    return _Step(
        "tool",
        {
            "toolCallId": str(uuid.uuid4()),
            "providerToolUseId": call_id,
            "toolName": name,
            "result": result,
            "error": None,
        },
        turn,
    )


def _history(steps: list[_Step]) -> list[NeutralMessage]:
    return _neutral_history_from_steps(steps)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- provider validators


def _anthropic_input(history: list[NeutralMessage]) -> list[dict[str, Any]]:
    return AnthropicClient._build_provider_messages(history)


def _openai_input(history: list[NeutralMessage]) -> list[dict[str, Any]]:
    items, _ = OpenAIResponsesClient._responses_input_from_messages(
        history, previous_response_id=None
    )
    return items


def _assert_anthropic_valid(wire: list[dict[str, Any]]) -> None:
    """Каждый `tool_use` ассистента закрыт `tool_result` В НАЧАЛЕ следующего user-сообщения."""
    for i, msg in enumerate(wire):
        if i > 0:
            assert not (msg["role"] == "user" and wire[i - 1]["role"] == "user"), wire
        if msg["role"] != "assistant":
            continue
        ids = [b["id"] for b in msg["content"] if b.get("type") == "tool_use"]
        if not ids:
            continue
        if i + 1 == len(wire):
            continue  # текущий ход: барьер ADR-025 ждёт настоящего результата
        nxt = wire[i + 1]
        assert nxt["role"] == "user", wire
        lead = []
        for block in nxt["content"]:
            if block.get("type") != "tool_result":
                break
            lead.append(block["tool_use_id"])
        assert sorted(lead) == sorted(ids), wire
        tail = [b for b in nxt["content"][len(lead) :] if b.get("type") == "tool_result"]
        assert tail == [], wire


def _assert_openai_valid(items: list[dict[str, Any]], *, open_ids: frozenset[str] = frozenset()):
    calls = [it["call_id"] for it in items if it.get("type") == "function_call"]
    for call_id in calls:
        outputs = [
            j
            for j, it in enumerate(items)
            if it.get("type") == "function_call_output" and it["call_id"] == call_id
        ]
        if call_id in open_ids:
            assert outputs == [], items
            continue
        assert len(outputs) == 1, items
        position = next(
            j for j, it in enumerate(items) if it.get("call_id") == call_id and "name" in it
        )
        assert outputs[0] > position, items


def _synthetic(msg: NeutralMessage) -> bool:
    return msg.role == "tool" and isinstance(msg.error, dict) and msg.error.get("code") == _MISSING


def _roles(history: list[NeutralMessage]) -> list[str]:
    return [("tool*" if _synthetic(m) else m.role) for m in history]


# --------------------------------------------------------------------------- §1–§2: history


def test_superseded_client_call_closed_synthetically_for_both_providers() -> None:
    """Кейс 1: брошенный `maps.geocode` + новое сообщение → синтетика, ввод валиден."""
    turn = uuid.uuid4()
    steps = [_user("где кафе", turn), _assistant(turn, ("toolu_A", "maps_geocode")), _user("ну?")]
    history = _history(steps)

    assert _roles(history) == ["user", "assistant", "tool*", "user"]
    synthetic = history[2]
    assert synthetic.provider_tool_use_id == "toolu_A"
    assert synthetic.error == {"code": _MISSING, "message": "Результат инструмента не получен."}

    wire = _anthropic_input(history)
    _assert_anthropic_valid(wire)
    last_user = wire[-1]["content"]
    assert [b["type"] for b in last_user] == ["tool_result", "text"]
    assert last_user[0]["is_error"] is True
    assert last_user[1]["text"] == "ну?"

    items = _openai_input(history)
    _assert_openai_valid(items)
    output = next(it for it in items if it.get("type") == "function_call_output")
    assert _MISSING in output["output"]


def test_poisoned_prod_form_revives() -> None:
    """Кейс 2: форма прод-инцидента (user; tool_use; user; turnFailed) + новое сообщение."""
    turn = uuid.uuid4()
    turn2 = uuid.uuid4()
    steps = [
        _user("где кафе", turn),
        _assistant(turn, ("call_A", "maps_geocode")),
        _user("повтор", turn2),
        _Step("assistant", {"content": [], "turnFailed": "UpstreamError"}, turn2),
        _user("ещё раз"),
    ]
    history = _history(steps)
    assert _roles(history) == ["user", "assistant", "tool*", "user", "assistant", "user"]
    _assert_anthropic_valid(_anthropic_input(history))
    _assert_openai_valid(_openai_input(history))


def test_intermediate_assistant_step_gets_synthetic_before_it() -> None:
    """ADR-114 §2 «промежуточный assistant-шаг»: синтетика встаёт сразу после хода с вызовом."""
    turn = uuid.uuid4()
    steps = [
        _user("где кафе", turn),
        _assistant(turn, ("call_A", "maps_geocode")),
        _Step("assistant", {"content": [{"type": "text", "text": "сбой"}]}, turn),
        _user("ещё раз"),
    ]
    history = _history(steps)
    assert _roles(history) == ["user", "assistant", "tool*", "assistant", "user"]
    _assert_openai_valid(_openai_input(history))


def test_history_build_is_idempotent_and_stateless() -> None:
    """Кейс 3 (часть без БД): сборка не пишет во вход и не держит состояния между сборками."""
    turn = uuid.uuid4()
    superseded = [_user("a", turn), _assistant(turn, ("toolu_X", "maps_geocode")), _user("b")]
    other_turn = uuid.uuid4()
    answered = [
        _user("a", other_turn),
        _assistant(other_turn, ("toolu_X", "maps_geocode")),
        _tool(other_turn, "toolu_X", "maps_geocode", {"ok": True}),
        _user("b"),
    ]
    before = copy.deepcopy([s.payload for s in superseded])

    first = _history(superseded)
    second = _history(superseded)
    assert first == second
    assert [s.payload for s in superseded] == before
    # Другая сессия с тем же provider id: её НАСТОЯЩИЙ результат не должен пропасть из-за
    # синтетики, выданной первой сборке.
    replay = _history(answered)
    assert _roles(replay) == ["user", "assistant", "tool", "user"]
    assert replay[2].result == {"ok": True}


def test_current_turn_calls_are_not_closed() -> None:
    """Кейс 4 (история): у текущего хода нет следующего `user` → синтетики нет."""
    turn = uuid.uuid4()
    steps = [
        _user("два файла", turn),
        _assistant(turn, ("toolu_A", "files_write"), ("toolu_B", "files_write")),
        _tool(turn, "toolu_A", "files.write", {"ok": True}),
    ]
    history = _history(steps)
    assert _roles(history) == ["user", "assistant", "tool"]
    assert not any(_synthetic(m) for m in history)
    _assert_openai_valid(_openai_input(history), open_ids=frozenset({"toolu_B"}))


def test_partially_answered_superseded_turn() -> None:
    """Кейс 5: из двух вызовов один с настоящим результатом, второй — синтетика, оба после хода."""
    turn = uuid.uuid4()
    steps = [
        _user("два файла", turn),
        _assistant(turn, ("toolu_A", "files_write"), ("toolu_B", "files_write")),
        _tool(turn, "toolu_A", "files.write", {"ok": True}),
        _user("забудь"),
    ]
    history = _history(steps)
    assert _roles(history) == ["user", "assistant", "tool", "tool*", "user"]
    assert history[2].provider_tool_use_id == "toolu_A"
    assert history[2].result == {"ok": True}
    assert history[3].provider_tool_use_id == "toolu_B"
    wire = _anthropic_input(history)
    _assert_anthropic_valid(wire)
    assert [b["type"] for b in wire[-1]["content"]] == ["tool_result", "tool_result", "text"]
    _assert_openai_valid(_openai_input(history))


def test_max_tokens_truncated_client_call_closed_from_replay_blocks() -> None:
    """Кейс 8: обрезанный ход (строк `tool_calls` нет) закрывается по блокам реплея."""
    turn = uuid.uuid4()
    steps = [
        _user("напиши файл", turn),
        _assistant(turn, ("toolu_T", "files_write"), text="Пишу файл"),
        _user("продолжай"),
    ]
    history = _history(steps)
    assert _roles(history) == ["user", "assistant", "tool*", "user"]
    _assert_anthropic_valid(_anthropic_input(history))
    _assert_openai_valid(_openai_input(history))


def test_interrupted_server_side_call_closed_too() -> None:
    """Кейс 9: server-side вызов без шага `tool` вытесняется наравне с клиентским."""
    turn = uuid.uuid4()
    steps = [
        _user("сделай сайт", turn),
        _assistant(turn, ("toolu_S", "site_write_file")),
        _user("алло"),
    ]
    history = _history(steps)
    assert _roles(history) == ["user", "assistant", "tool*", "user"]
    assert history[2].tool_name == "site_write_file"
    _assert_openai_valid(_openai_input(history))


def test_tool_step_after_later_user_step_is_dropped() -> None:
    """Кейс 10: шаг `tool`, записанный в гонке после нового `user`, из реплея отбрасывается."""
    turn = uuid.uuid4()
    steps = [
        _user("где кафе", turn),
        _assistant(turn, ("call_A", "maps_geocode")),
        _user("ну?"),
        _tool(turn, "call_A", "maps.geocode", {"lat": 1}),
        _user("ещё"),
    ]
    history = _history(steps)
    assert _roles(history) == ["user", "assistant", "tool*", "user", "user"]
    items = _openai_input(history)
    outputs = [it for it in items if it.get("type") == "function_call_output"]
    assert len(outputs) == 1 and _MISSING in outputs[0]["output"]
    _assert_openai_valid(items)
    wire = _anthropic_input(history)
    _assert_anthropic_valid(wire)
    assert sum(b.get("type") == "tool_result" for m in wire for b in m["content"]) == 1


def test_anthropic_merges_parallel_tool_results_into_one_user_message() -> None:
    """ADR-114 §2: подряд идущие результаты одного хода — одно user-сообщение."""
    turn = uuid.uuid4()
    steps = [
        _user("два файла", turn),
        _assistant(turn, ("toolu_A", "files_write"), ("toolu_B", "files_write")),
        _tool(turn, "toolu_A", "files.write", {"ok": 1}),
        _tool(turn, "toolu_B", "files.write", {"ok": 2}),
    ]
    wire = _anthropic_input(_history(steps))
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in wire[2]["content"]] == ["toolu_A", "toolu_B"]


# --------------------------------------------------------------------------- §3: /tool-result


@dataclass
class _Call:
    id: uuid.UUID
    session_id: uuid.UUID
    message_step_id: uuid.UUID
    tool_name: str
    provider_tool_use_id: str
    status: str = "pending"
    args: dict[str, Any] = field(default_factory=dict)


class _FakeRepo:
    """Ровно тот срез `ChatRepository`, который читает `ChatOrchestrator.tool_result`."""

    def __init__(self, session_id: uuid.UUID) -> None:
        self.session_id = session_id
        self.steps: list[_Step] = []
        self.calls: dict[uuid.UUID, _Call] = {}
        self.writes: list[tuple[str, Any]] = []
        self.saved_continuation: _Step | None = None
        self.on_tool_step: Any = None

    async def get_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> Any:
        return SimpleNamespace(id=session_id, generation_backend=None)

    async def get_tool_call(self, tool_call_id: uuid.UUID) -> _Call | None:
        return self.calls.get(tool_call_id)

    async def list_tool_calls_for_step(self, session_id: uuid.UUID, turn: uuid.UUID) -> list:
        return [copy.copy(c) for c in self.calls.values() if c.message_step_id == turn]

    async def assistant_tool_step_id(self, session_id: uuid.UUID, turn: uuid.UUID) -> Any:
        mine = [s for s in self.steps if s.role == "assistant" and s.message_step_id == turn]
        return mine[-1].id if mine else None

    async def list_steps(self, session_id: uuid.UUID) -> list[_Step]:
        return list(self.steps)

    async def complete_tool_call(
        self, *, tool_call_id: uuid.UUID, status: str, result: Any
    ) -> bool:
        call = self.calls[tool_call_id]
        if call.status != "pending":
            return False
        call.status = status
        self.writes.append(("tool_call", tool_call_id))
        return True

    async def add_step(self, *, session_id, message_step_id, role, payload, usage=None) -> _Step:
        step = _Step(role, payload, message_step_id)
        self.steps.append(step)
        self.writes.append(("step", role))
        if role == "tool" and self.on_tool_step is not None:
            self.on_tool_step()
        return step

    async def next_step_after(self, session_id, turn, anchor) -> _Step | None:
        return self.saved_continuation


class _ContinuationReached(Exception):
    pass


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _orchestrator(repo: _FakeRepo) -> ChatOrchestrator:
    orch = object.__new__(ChatOrchestrator)
    orch._deps = SimpleNamespace(repo=repo, audit=SimpleNamespace(record=_noop))  # type: ignore[attr-defined]
    orch._session = _Session()  # type: ignore[assignment]

    async def _no_backend_check(*_a: Any, **_k: Any) -> None:
        return None

    async def _decorate(out: Any, **_k: Any) -> Any:
        return out

    async def _evaluate(*_a: Any, **_k: Any) -> Any:
        raise _ContinuationReached

    orch._ensure_session_backend = _no_backend_check  # type: ignore[method-assign]
    orch._decorate_turn_out = _decorate  # type: ignore[method-assign]
    orch._evaluate = _evaluate  # type: ignore[method-assign]
    orch._render_saved_step = lambda _s, _t, saved: SimpleNamespace(  # type: ignore[method-assign]
        status="replayed", step=saved
    )
    return orch


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


def _turn_with_calls(*names: str) -> tuple[_FakeRepo, uuid.UUID, list[_Call]]:
    session_id = uuid.uuid4()
    repo = _FakeRepo(session_id)
    turn = uuid.uuid4()
    calls = [
        _Call(uuid.uuid4(), session_id, turn, "maps.geocode", f"call_{name}") for name in names
    ]
    repo.steps += [
        _user("где кафе", turn),
        _assistant(turn, *((c.provider_tool_use_id, "maps_geocode") for c in calls)),
    ]
    for c in calls:
        repo.calls[c.id] = c
    return repo, turn, calls


def _result(call: _Call) -> list[ToolResultIn]:
    return [ToolResultIn(tool_call_id=call.id, result={"lat": 1}, error=None)]


async def _tool_result(repo: _FakeRepo, call: _Call) -> Any:
    return await _orchestrator(repo).tool_result(
        user_id=uuid.uuid4(), session_id=repo.session_id, results=_result(call)
    )


async def test_tool_result_of_current_turn_keeps_barrier() -> None:
    """Кейс 4 (/tool-result): нового `user` нет → `tool_call` с оставшимся вызовом."""
    repo, _turn, (a, b) = _turn_with_calls("A", "B")
    out = await _tool_result(repo, a)
    assert out.status == "tool_call"
    assert [c.id for c in out.tool_calls] == [str(b.id)]


@pytest.mark.parametrize("backend", ["legacy", "v2"])
async def test_late_result_on_superseded_turn_is_409_before_any_write(backend: str) -> None:
    """Кейс 6: запоздалый результат → `409`, `code == "conflict"`, ничего не записано."""
    repo, _turn, (a,) = _turn_with_calls("A")
    repo.steps.append(_user("ну?"))
    orch = _orchestrator(repo)
    repo.generation_mode_for_message_step = _general  # type: ignore[attr-defined]
    with pytest.raises(ConflictError) as exc:
        await orch.tool_result(
            user_id=uuid.uuid4(),
            session_id=repo.session_id,
            results=_result(a),
            generation_backend=backend,  # type: ignore[arg-type]
        )
    assert exc.value.code == "conflict"
    assert exc.value.status_code == 409
    assert repo.writes == []
    assert a.status == "pending"


async def _general(*_a: Any, **_k: Any) -> str:
    return "general"


async def test_closed_turn_keeps_adr025_idempotency_after_new_message() -> None:
    """Кейс 7: барьер закрыт, continuation сохранён, новое сообщение → повтор идемпотентен."""
    repo, turn, (a,) = _turn_with_calls("A")
    a.status = "completed"
    repo.steps.append(_tool(turn, "call_A", "maps.geocode", {"lat": 1}))
    continuation = _assistant(turn, text="кафе рядом")
    repo.steps += [continuation, _user("спасибо")]
    repo.saved_continuation = continuation
    out = await _tool_result(repo, a)
    assert out.status == "replayed"
    assert out.step is continuation
    assert repo.writes == []


async def test_user_step_landing_between_checks_is_409_without_continuation() -> None:
    """Кейс 11 (часть без БД): `user` вставлен после записи шага `tool` → проверка 2 → `409`."""
    repo, _turn, (a,) = _turn_with_calls("A")
    repo.on_tool_step = lambda: repo.steps.append(_user("новое, в гонке"))
    with pytest.raises(ConflictError) as exc:
        await _tool_result(repo, a)
    assert exc.value.code == "conflict"
    # Запись состоялась ДО проверки 2 (её откат — свойство сессии запроса, интеграционный кейс),
    # а continuation не начат: `_evaluate` не вызван, иначе поднялся бы `_ContinuationReached`.
    assert ("step", "tool") in repo.writes


async def test_retry_of_completed_call_of_superseded_open_turn_is_409() -> None:
    """Кейс 12: A прислан, затем новое сообщение, затем повтор A при `pending` B → `409`."""
    repo, turn, (a, _b) = _turn_with_calls("A", "B")
    a.status = "completed"
    repo.steps += [_tool(turn, "call_A", "maps.geocode", {"lat": 1}), _user("забудь")]
    with pytest.raises(ConflictError):
        await _tool_result(repo, a)
    assert repo.writes == []


# --------------------------------------------------------------------------- OpenAPI


@pytest.mark.parametrize("path", ["/v1/chat/tool-result", "/v1/chat/v2/tool-result"])
def test_openapi_documents_409_on_both_tool_result_routes(path: str) -> None:
    from app.main import app

    responses = app.openapi()["paths"][path]["post"]["responses"]
    assert "409" in responses
    assert "`conflict`" in responses["409"]["description"]
