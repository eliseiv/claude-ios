"""Integration: брошенный вызов инструмента и новое сообщение пользователя (ADR-114).

Норма — ``docs/modules/chat-orchestrator/09-testing.md`` §«Брошенный клиентский вызов и новое
сообщение (ADR-114)». Здесь — кейсы, которым нужны реальная БД и HTTP (кейсы 1, 2, 3, 6, 8, 11);
кейсы, выразимые без БД, — в ``tests/unit/test_chat_superseded_tool_calls_adr114.py``.

Техника — как в ``test_provider_input_shape_adr105.py``: поднимаются НАСТОЯЩИЕ клиенты
``AnthropicClient`` / ``OpenAIClient`` / ``OpenAIResponsesClient``, у их SDK подменён только
транспорт (``httpx.MockTransport``). Поэтому ассерт «ввод провайдера валиден» идёт по телу
исходящего HTTP-запроса — ровно тому, что в проде отверг OpenAI (``400 No tool output found``).

Варианты кейсов 1–3: оба бэкенда × оба провайдера — ``legacy``/Anthropic, ``v2``/Anthropic,
``legacy``/Responses (инстанс с ``CHAT_LEGACY_WEB_SEARCH_ENABLED``), ``v2``/Responses; плюс
``legacy``/Chat Completions — путь legacy-ручки OpenAI-инстанса по умолчанию.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx
import openai
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.chat.anthropic_client as anthropic_mod
import app.chat.llm_client as llm_mod
from app.chat.anthropic_client import AnthropicClient
from app.chat.openai_client import OpenAIClient
from app.chat.openai_responses_client import OpenAIResponsesClient
from app.chat.repository import ChatRepository
from app.config import Settings, get_settings
from tests.conftest import auth_headers, seed_user
from tests.integration.test_provider_input_shape_adr105 import (
    CLAUDE,
    GPT,
    _Upstream,
    anthropic_credit_exhausted,
    anthropic_tool_use,
    chat_tool_call,
    responses_function_call,
)

ANTHROPIC_KEY = "sk-ant-service-test"
OPENAI_KEY = "sk-openai-service-test"

_MISSING = "tool_result_missing"
_MISSING_TEXT = "Результат инструмента не получен."


# ============================ variants ============================


@dataclass(frozen=True)
class _Variant:
    """Бэкенд ручки × провайдер, который читает историю."""

    v2: bool
    kind: str  # "anthropic" | "responses" | "chat" — ключ ``_Upstream``

    @property
    def id(self) -> str:
        return f"{'v2' if self.v2 else 'legacy'}-{self.kind}"


_VARIANTS = [
    _Variant(v2=False, kind="anthropic"),
    _Variant(v2=True, kind="anthropic"),
    _Variant(v2=False, kind="responses"),
    _Variant(v2=True, kind="responses"),
    _Variant(v2=False, kind="chat"),
]
_VARIANT_IDS = [v.id for v in _VARIANTS]


def _body_key(kind: str) -> str:
    return "input" if kind == "responses" else "messages"


def _configure(variant: _Variant, cfg: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cfg, "llm_provider", "anthropic" if variant.kind == "anthropic" else "openai"
    )
    if variant.kind == "responses" and not variant.v2:
        # Legacy-ручка реплеит через Responses-клиента, когда инстанс включил этот флаг.
        monkeypatch.setattr(cfg, "chat_legacy_web_search_enabled", True)


def _script_tool_call(upstream: _Upstream, variant: _Variant, call_id: str) -> None:
    """Первый ход — клиентский ``files.read`` в форме провайдера варианта."""
    if variant.kind == "anthropic":
        upstream.scripts["anthropic"] = [anthropic_tool_use(call_id)]
    elif variant.kind == "responses":
        upstream.scripts["responses"] = [
            responses_function_call(call_id, "files_read", '{"path": "a.txt"}')
        ]
    else:
        upstream.scripts["chat"] = [chat_tool_call(call_id)]


def _call_id(variant: _Variant) -> str:
    return "toolu_A114" if variant.kind == "anthropic" else "call_A114"


# ============================ fixtures ============================


@pytest.fixture
def upstream(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> _Upstream:
    """НАСТОЯЩИЕ клиенты, чьи SDK говорят с ``_Upstream`` вместо сети (как в ADR-105)."""
    up = _Upstream()
    transport = httpx.MockTransport(up.handle)
    anth = AnthropicClient()
    anth._client = anthropic.AsyncAnthropic(
        api_key="placeholder", max_retries=0, http_client=httpx.AsyncClient(transport=transport)
    )
    chat = OpenAIClient()
    chat._client = openai.AsyncOpenAI(
        api_key="placeholder", max_retries=0, http_client=httpx.AsyncClient(transport=transport)
    )
    responses = OpenAIResponsesClient()
    responses._client = openai.AsyncOpenAI(
        api_key="placeholder", max_retries=0, http_client=httpx.AsyncClient(transport=transport)
    )
    monkeypatch.setattr(anthropic_mod, "_anthropic_singleton", anth)
    monkeypatch.setattr(llm_mod, "_openai_singleton", chat)
    monkeypatch.setattr(llm_mod, "_openai_responses_singleton", responses)
    return up


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Кэшированные Settings с герметичной двухпровайдерной базой; каждое поле восстановится."""
    s = get_settings()
    for name, value in {
        "llm_provider": "anthropic",
        "llm_providers_raw": "",
        "anthropic_api_key": ANTHROPIC_KEY,
        "anthropic_api_key_backup": "",
        "openai_api_key": OPENAI_KEY,
        "openai_api_key_backup": "",
        "anthropic_model": CLAUDE,
        "openai_model": GPT,
        "anthropic_models_raw": json.dumps({CLAUDE: "Claude Sonnet 4.5"}),
        "openai_models_raw": json.dumps({GPT: "GPT-4o"}),
        "anthropic_chat_fallback_openai_model": "",
        "openai_chat_fallback_anthropic_model": "",
        "chat_legacy_web_search_enabled": False,
        "byok_default_model": CLAUDE,
        "openai_byok_default_model": GPT,
        "fal_api_key": "",
    }.items():
        monkeypatch.setattr(s, name, value)
    return s


# ============================ HTTP helpers ============================


async def _credits_user(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with maker() as s:
        return await seed_user(s, subscription="active", balance=50)


async def _post_run(
    client: AsyncClient,
    uid: uuid.UUID,
    *,
    v2: bool,
    message: str,
    session_id: str | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"userId": str(uid), "message": message, "mode": "credits"}
    if session_id is not None:
        body["sessionId"] = session_id
    if v2:
        body["generationMode"] = "general"
    return await client.post(
        "/v1/chat/v2/run" if v2 else "/v1/chat/run", json=body, headers=auth_headers(uid)
    )


async def _run_ok(
    client: AsyncClient,
    uid: uuid.UUID,
    *,
    v2: bool,
    message: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    r = await _post_run(client, uid, v2=v2, message=message, session_id=session_id)
    # Главный ассерт регресса: 200 по обычному контракту, а не 502 (ADR-114 «Последствия»).
    assert r.status_code == 200, r.text
    payload: dict[str, Any] = r.json()
    return payload


async def _post_tool_result(
    client: AsyncClient, uid: uuid.UUID, session_id: str, tool_call_id: str, *, v2: bool
) -> httpx.Response:
    return await client.post(
        "/v1/chat/v2/tool-result" if v2 else "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": session_id,
            "toolCallId": tool_call_id,
            "result": {"content": "file body"},
        },
        headers=auth_headers(uid),
    )


async def _abandoned_turn(
    client: AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    variant: _Variant,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Ход вернул ``status=tool_call`` клиентского инструмента; результат НЕ присылается."""
    _script_tool_call(upstream, variant, _call_id(variant))
    uid = await _credits_user(maker)
    run = await _run_ok(client, uid, v2=variant.v2, message="read a.txt")
    assert run["status"] == "tool_call", run
    return uid, run


# ============================ DB helpers ============================


async def _scalar(maker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> Any:
    async with maker() as s:
        return await s.scalar(text(sql), params)


async def _tool_steps(maker: async_sessionmaker[AsyncSession], session_id: str) -> int:
    return int(
        await _scalar(
            maker,
            "SELECT count(*) FROM chat_steps WHERE session_id = :s AND role = 'tool'",
            s=session_id,
        )
        or 0
    )


async def _user_steps(maker: async_sessionmaker[AsyncSession], session_id: str) -> int:
    return int(
        await _scalar(
            maker,
            "SELECT count(*) FROM chat_steps WHERE session_id = :s AND role = 'user'",
            s=session_id,
        )
        or 0
    )


async def _tool_call_status(maker: async_sessionmaker[AsyncSession], tool_call_id: str) -> str:
    return str(await _scalar(maker, "SELECT status FROM tool_calls WHERE id = :i", i=tool_call_id))


async def _tool_call_rows(maker: async_sessionmaker[AsyncSession], session_id: str) -> int:
    return int(
        await _scalar(maker, "SELECT count(*) FROM tool_calls WHERE session_id = :s", s=session_id)
        or 0
    )


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    return int(await _scalar(maker, "SELECT balance FROM wallets WHERE user_id = :u", u=str(uid)))


# ============================ provider-input validators ============================


def _assert_anthropic_closed(messages: list[dict[str, Any]], call_id: str) -> None:
    """Anthropic: ``tool_use`` закрыт РОВНО одним ``tool_result`` в начале СЛЕДУЮЩЕГО
    user-сообщения; синтетический несёт ``is_error``; двух user-сообщений подряд нет."""
    for i in range(1, len(messages)):
        assert not (messages[i]["role"] == "user" and messages[i - 1]["role"] == "user"), messages
    idx = next(
        i
        for i, m in enumerate(messages)
        if m["role"] == "assistant"
        and any(
            isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == call_id
            for b in m["content"]
        )
    )
    nxt = messages[idx + 1]
    assert nxt["role"] == "user", messages
    lead: list[dict[str, Any]] = []
    for block in nxt["content"]:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            break
        lead.append(block)
    ids = [
        b["id"]
        for b in messages[idx]["content"]
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    assert sorted(b["tool_use_id"] for b in lead) == sorted(ids), messages
    results = [
        b
        for m in messages
        for b in (m["content"] if isinstance(m["content"], list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result" and b["tool_use_id"] == call_id
    ]
    assert len(results) == 1, messages
    assert results[0]["is_error"] is True
    assert results[0]["content"] == _MISSING_TEXT


def _assert_responses_closed(items: list[dict[str, Any]], call_id: str) -> None:
    """Responses: у ``function_call`` ровно один ``function_call_output`` ПОСЛЕ него."""
    position = next(
        j
        for j, it in enumerate(items)
        if it.get("type") == "function_call" and it["call_id"] == call_id
    )
    outputs = [
        j
        for j, it in enumerate(items)
        if it.get("type") == "function_call_output" and it["call_id"] == call_id
    ]
    assert len(outputs) == 1, items
    assert outputs[0] > position, items
    assert _MISSING in items[outputs[0]]["output"], items
    called = {it["call_id"] for it in items if it.get("type") == "function_call"}
    answered = {it["call_id"] for it in items if it.get("type") == "function_call_output"}
    assert called == answered, items


def _assert_chat_closed(messages: list[dict[str, Any]], call_id: str) -> None:
    """Chat Completions: ровно одно ``role=tool`` с ``tool_call_id`` ПОСЛЕ вызова."""
    position = next(
        i
        for i, m in enumerate(messages)
        if any(c["id"] == call_id for c in (m.get("tool_calls") or []))
    )
    tools = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "tool" and m["tool_call_id"] == call_id
    ]
    assert len(tools) == 1, messages
    assert tools[0] > position, messages
    assert _MISSING in messages[tools[0]]["content"], messages
    called = {c["id"] for m in messages for c in (m.get("tool_calls") or [])}
    answered = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    assert called == answered, messages


_VALIDATORS: dict[str, Callable[[list[dict[str, Any]], str], None]] = {
    "anthropic": _assert_anthropic_closed,
    "responses": _assert_responses_closed,
    "chat": _assert_chat_closed,
}


def _only_body(upstream: _Upstream, kind: str) -> list[dict[str, Any]]:
    """Ровно один вызов провайдера варианта; вернуть его ввод (``messages``/``input``)."""
    assert upstream.kinds() == [kind], upstream.kinds()
    entries: list[dict[str, Any]] = upstream.bodies(kind)[0][_body_key(kind)]
    return entries


# ======================= Кейс 1 — регресс прод-инцидента =======================


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
async def test_case1_abandoned_client_call_then_new_message_is_200_with_paired_input(
    variant: _Variant,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ход вернул ``tool_call``, результат не прислан → новое сообщение → ``200``, ввод парный.

    Diff-стойкость (мутация из docs: снять синтетическое закрытие): вызов уходит провайдеру без
    результата — валидатор варианта падает на «ровно один результат». Не запускалось локально
    (Docker приостановлен владельцем), прогон — в Actions.
    """
    _configure(variant, cfg, monkeypatch)
    uid, run = await _abandoned_turn(client, db_sessionmaker, upstream, variant)
    upstream.calls.clear()

    second = await _run_ok(
        client, uid, v2=variant.v2, message="ну что там?", session_id=run["sessionId"]
    )

    assert second["status"] == "assistant_message", second
    wire = _only_body(upstream, variant.kind)
    _VALIDATORS[variant.kind](wire, _call_id(variant))
    if variant.kind == "anthropic":
        # tool_result — в НАЧАЛЕ user-сообщения, перед текстом нового вопроса (ADR-114 §2).
        last = wire[-1]
        assert last["role"] == "user", wire
        assert [b["type"] for b in last["content"]] == ["tool_result", "text"], last
        assert last["content"][1]["text"] == "ну что там?"


# ======================= Кейс 2 — «отравленный» чат оживает =======================


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
async def test_case2_poisoned_prod_form_revives_on_next_message(
    variant: _Variant,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Форма прод-инцидента в БД: ``user``; ``assistant`` с ``tool_use``; шага ``tool`` нет;
    ``user`` (повтор вопроса); ``assistant`` с ``turnFailed`` → следующее сообщение → ``200``.

    Первые два шага пишет реальный путь ручки; повтор вопроса и пометку отказа — фикстура, в той
    форме, в какой их пишут ``add_step`` и пометка ``turnFailed`` оркестратора. Diff-стойкость
    (снять синтетическое закрытие) — как у кейса 1. Не запускалось локально.
    """
    _configure(variant, cfg, monkeypatch)
    uid, run = await _abandoned_turn(client, db_sessionmaker, upstream, variant)
    session_id = uuid.UUID(run["sessionId"])
    failed_turn = uuid.uuid4()
    async with db_sessionmaker() as s:
        repo = ChatRepository(s)
        await repo.add_step(
            session_id=session_id,
            message_step_id=failed_turn,
            role="user",
            payload={"content": [{"type": "text", "text": "read a.txt"}]},
        )
        await repo.add_step(
            session_id=session_id,
            message_step_id=failed_turn,
            role="assistant",
            payload={
                "content": [{"type": "text", "text": "Не удалось получить ответ."}],
                "turnFailed": {"reason": "UpstreamError"},
            },
        )
        await s.commit()
    upstream.calls.clear()

    revived = await _run_ok(
        client, uid, v2=variant.v2, message="ещё раз", session_id=run["sessionId"]
    )

    assert revived["status"] == "assistant_message", revived
    wire = _only_body(upstream, variant.kind)
    _VALIDATORS[variant.kind](wire, _call_id(variant))
    assert await _tool_steps(db_sessionmaker, run["sessionId"]) == 0


# ======================= Кейс 3 — без записи в БД, идемпотентно =======================


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
async def test_case3_closing_writes_nothing_and_rebuilds_the_same_input(
    variant: _Variant,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """После кейса 1: нового шага ``tool`` нет, ``tool_calls.status`` = ``pending``; две
    последовательные сборки истории дают одинаковый ввод (второй ход реплеит первый как префикс).

    Diff-стойкость: запись синтетики в ``chat_steps`` роняет счётчик шагов ``tool``; смена
    статуса вызова — ассерт ``pending``. Не запускалось локально.
    """
    _configure(variant, cfg, monkeypatch)
    uid, run = await _abandoned_turn(client, db_sessionmaker, upstream, variant)
    upstream.calls.clear()

    await _run_ok(client, uid, v2=variant.v2, message="ну?", session_id=run["sessionId"])
    first = _only_body(upstream, variant.kind)
    assert await _tool_steps(db_sessionmaker, run["sessionId"]) == 0
    assert await _tool_call_status(db_sessionmaker, run["toolCall"]["id"]) == "pending"

    upstream.calls.clear()
    await _run_ok(client, uid, v2=variant.v2, message="и ещё", session_id=run["sessionId"])
    second = _only_body(upstream, variant.kind)

    assert second[: len(first)] == first, (first, second)
    assert await _tool_steps(db_sessionmaker, run["sessionId"]) == 0
    assert await _tool_call_status(db_sessionmaker, run["toolCall"]["id"]) == "pending"


# ======================= Кейс 6 — запоздалый результат → 409 =======================


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _VARIANTS, ids=_VARIANT_IDS)
async def test_case6_late_tool_result_on_superseded_turn_is_409_conflict(
    variant: _Variant,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """После кейса 1 ``/chat/tool-result`` (и ``/chat/v2/tool-result``) на брошенный вызов →
    ``409``, ``error.code == "conflict"``; шаг ``tool`` не записан, провайдер не вызван, баланс
    не изменился.

    Diff-стойкость (мутация из docs: снять проверку 1): вызов в ``pending`` принимается, пишется
    шаг ``tool`` и выполняется continuation → ``200`` вместо ``409``. Не запускалось локально.
    """
    _configure(variant, cfg, monkeypatch)
    uid, run = await _abandoned_turn(client, db_sessionmaker, upstream, variant)
    await _run_ok(client, uid, v2=variant.v2, message="забудь", session_id=run["sessionId"])
    balance_before = await _balance(db_sessionmaker, uid)
    upstream.calls.clear()

    r = await _post_tool_result(client, uid, run["sessionId"], run["toolCall"]["id"], v2=variant.v2)

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "conflict", r.text
    assert upstream.calls == []
    assert await _tool_steps(db_sessionmaker, run["sessionId"]) == 0
    assert await _balance(db_sessionmaker, uid) == balance_before
    assert await _tool_call_status(db_sessionmaker, run["toolCall"]["id"]) == "pending"


# ======================= Кейс 8 — обрезанный по max_tokens ход =======================


def _anthropic_max_tokens_with_client_call(tool_id: str) -> httpx.Response:
    """Ход, обрезанный по ``max_tokens``: частичный текст + НЕзавершённый ``files_write``."""
    return httpx.Response(
        200,
        json={
            "id": "msg_trunc",
            "type": "message",
            "role": "assistant",
            "model": CLAUDE,
            "content": [
                {"type": "text", "text": "Пишу файл"},
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "files_write",
                    "input": {"path": "index.html"},
                },
            ],
            "stop_reason": "max_tokens",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 16000},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("v2", "reader"),
    [(False, "anthropic"), (True, "anthropic"), (True, "responses")],
    ids=["legacy-anthropic", "v2-anthropic", "v2-failover-responses"],
)
async def test_case8_max_tokens_truncated_client_call_closed_from_replay_blocks(
    v2: bool,
    reader: str,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ход вернул ``blocked``/``max_tokens`` с ``tool_use`` клиентского ``files.write``; строк
    ``tool_calls`` у хода нет → новое сообщение → ``200``, обрезанный вызов закрыт синтетикой.

    Оба провайдера читают историю: Anthropic — напрямую; OpenAI Responses — через failover
    Claude-сессии (баланс Anthropic исчерпан, ``ANTHROPIC_CHAT_FALLBACK_OPENAI_MODEL`` задан).

    Diff-стойкость (мутация из docs: брать открытые вызовы из ``tool_calls.status = pending``
    вместо блоков реплея): строк ``tool_calls`` нет (ассерт ниже), вызов уходит без результата,
    валидатор падает. Не запускалось локально.
    """
    monkeypatch.setattr(cfg, "llm_provider", "anthropic")
    upstream.scripts["anthropic"] = [_anthropic_max_tokens_with_client_call("toolu_T114")]
    if reader == "responses":
        monkeypatch.setattr(cfg, "anthropic_chat_fallback_openai_model", GPT)
        upstream.scripts["anthropic"].append(anthropic_credit_exhausted())
    uid = await _credits_user(db_sessionmaker)

    truncated = await _run_ok(client, uid, v2=v2, message="сделай лендинг")
    assert truncated["status"] == "blocked", truncated
    assert truncated["blockReason"] == "max_tokens", truncated
    assert await _tool_call_rows(db_sessionmaker, truncated["sessionId"]) == 0
    upstream.calls.clear()

    nxt = await _run_ok(client, uid, v2=v2, message="продолжай", session_id=truncated["sessionId"])

    assert nxt["status"] == "assistant_message", nxt
    if reader == "anthropic":
        _assert_anthropic_closed(_only_body(upstream, "anthropic"), "toolu_T114")
    else:
        assert upstream.kinds() == ["anthropic", "responses"], upstream.kinds()
        _assert_responses_closed(upstream.bodies("responses")[0]["input"], "toolu_T114")


# ======================= Кейс 11 — повторная проверка вытеснения =======================


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [False, True], ids=["legacy", "v2"])
async def test_case11_user_step_committed_between_write_and_check2_rolls_back_and_409(
    v2: bool,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/chat/tool-result`` прошёл проверку 1 → записал шаг ``tool`` → в ДРУГОЙ транзакции
    вставлен И ЗАКОММИЧЕН ``user``-шаг → проверка 2 → ``409 conflict``; провайдер не вызван,
    баланс не изменился; шаг ``tool`` откатан, вызов ``pending``; повтор → ``409``; следующее
    сообщение → ``200``, у вызова ровно один результат — синтетический.

    Порядок детерминирован патчем ``ChatRepository.add_step`` (не реальная параллельность).
    Diff-стойкость (мутация из docs: предикат проверки 2 заменить предикатом §1): записанный шаг
    ``tool`` лежит ДО нового ``user``, по §1 вызов не вытеснен → continuation → ``200``.
    Не запускалось локально.
    """
    variant = _Variant(v2=v2, kind="anthropic")
    _configure(variant, cfg, monkeypatch)
    uid, run = await _abandoned_turn(client, db_sessionmaker, upstream, variant)
    session_id = run["sessionId"]
    balance_before = await _balance(db_sessionmaker, uid)
    users_before = await _user_steps(db_sessionmaker, session_id)

    original_add_step = ChatRepository.add_step
    armed = {"on": True}

    async def _add_step_then_commit_user(self: ChatRepository, **kwargs: Any) -> Any:
        step = await original_add_step(self, **kwargs)
        if kwargs.get("role") == "tool" and armed["on"]:
            armed["on"] = False
            async with db_sessionmaker() as other:
                await original_add_step(
                    ChatRepository(other),
                    session_id=uuid.UUID(session_id),
                    message_step_id=uuid.uuid4(),
                    role="user",
                    payload={"content": [{"type": "text", "text": "новое, в гонке"}]},
                )
                await other.commit()
        return step

    hook: Callable[..., Awaitable[Any]] = _add_step_then_commit_user
    monkeypatch.setattr(ChatRepository, "add_step", hook)
    upstream.calls.clear()

    r = await _post_tool_result(client, uid, session_id, run["toolCall"]["id"], v2=v2)

    assert armed["on"] is False, "хук не сработал: шаг tool не писался"
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "conflict", r.text
    assert upstream.calls == []
    assert await _balance(db_sessionmaker, uid) == balance_before
    assert await _tool_steps(db_sessionmaker, session_id) == 0  # запись запроса откатана
    assert await _user_steps(db_sessionmaker, session_id) == users_before + 1
    assert await _tool_call_status(db_sessionmaker, run["toolCall"]["id"]) == "pending"

    again = await _post_tool_result(client, uid, session_id, run["toolCall"]["id"], v2=v2)
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "conflict", again.text
    assert upstream.calls == []

    nxt = await _run_ok(client, uid, v2=v2, message="следующее", session_id=session_id)
    assert nxt["status"] == "assistant_message", nxt
    _assert_anthropic_closed(_only_body(upstream, "anthropic"), _call_id(variant))
