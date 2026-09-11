"""Integration: вход вызова LLM — в форме провайдера клиента, который его отправляет (ADR-105 §A).

Норма — ``docs/modules/chat-orchestrator/09-testing.md`` §«Integration — вход вызова в форме
провайдера клиента (ADR-105)» и ``docs/06-testing-strategy.md`` §«Отказ поставщика (ADR-105)».

**Техника — главное требование раздела.** Рендер вложений живёт ВНУТРИ клиента, поэтому здесь
поднимаются НАСТОЯЩИЕ классы ``AnthropicClient`` / ``OpenAIClient`` / ``OpenAIResponsesClient``; их
SDK-клиенту подменён только транспорт (``httpx.MockTransport``) — тело исходящего запроса
перехватывается на границе HTTP, и ассерт идёт по нему, а не по коду ответа ручки. Фейковый
``LLMClient`` заменил бы клиента целиком — вместе с единственным местом рендера.

История (§A3) пишется в ``chat_steps`` РЕАЛЬНЫМИ клиентами на реальном пути ручки (их
``LLMResult.content_blocks`` сохраняет оркестратор) и затем реплеится другим клиентом.
"""

from __future__ import annotations

import base64
import io
import json
import uuid
from collections.abc import Iterator
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
from app.config import Settings, get_settings
from tests.conftest import auth_headers, seed_user

ANTHROPIC_KEY = "sk-ant-service-test"
OPENAI_KEY = "sk-openai-service-test"
CLAUDE = "claude-sonnet-4-5"
GPT = "gpt-4o"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_TEXT_FILE = "line one\nline two"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


_PNG_B64 = _b64(_PNG)
_PDF_B64 = _b64(_pdf_bytes())
_PNG_URI = f"data:image/png;base64,{_PNG_B64}"
_PDF_URI = f"data:application/pdf;base64,{_PDF_B64}"


def _png_attachment() -> dict[str, str]:
    return {"type": "image", "mediaType": "image/png", "filename": "p.png", "data": _PNG_B64}


def _pdf_attachment() -> dict[str, str]:
    return {
        "type": "document",
        "mediaType": "application/pdf",
        "filename": "d.pdf",
        "data": _PDF_B64,
    }


def _text_attachment() -> dict[str, str]:
    return {
        "type": "text",
        "mediaType": "text/plain",
        "filename": "n.txt",
        "data": _b64(_TEXT_FILE.encode("utf-8")),
    }


# ============================ the provider HTTP boundary ============================


class _Upstream:
    """Scripted HTTP boundary for the three REAL clients; records every outgoing request body.

    Routing is by URL path — ``/v1/messages`` (Anthropic), ``/chat/completions`` (Chat
    Completions), ``/responses`` (Responses API). An unscripted call answers a plain final text.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.scripts: dict[str, list[httpx.Response]] = {
            "anthropic": [],
            "chat": [],
            "responses": [],
        }

    @staticmethod
    def _kind(url: httpx.URL) -> str:
        path = url.path
        if path.endswith("/messages"):
            return "anthropic"
        if path.endswith("/chat/completions"):
            return "chat"
        if path.endswith("/responses"):
            return "responses"
        raise AssertionError(f"unexpected upstream call {url}")

    def handle(self, request: httpx.Request) -> httpx.Response:
        kind = self._kind(request.url)
        self.calls.append((kind, json.loads(request.content)))
        queue = self.scripts[kind]
        if queue:
            return queue.pop(0)
        return _DEFAULT[kind]()

    def bodies(self, kind: str) -> list[dict[str, Any]]:
        return [body for k, body in self.calls if k == kind]

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


def anthropic_text(text_value: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_ok",
            "type": "message",
            "role": "assistant",
            "model": CLAUDE,
            "content": [{"type": "text", "text": text_value}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


def anthropic_tool_use(tool_id: str, *, thinking: bool = False) -> httpx.Response:
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "let me look", "signature": "sig-1"})
    content.append({"type": "text", "text": "Reading the file."})
    content.append(
        {"type": "tool_use", "id": tool_id, "name": "files_read", "input": {"path": "a.txt"}}
    )
    return httpx.Response(
        200,
        json={
            "id": "msg_tool",
            "type": "message",
            "role": "assistant",
            "model": CLAUDE,
            "content": content,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


def anthropic_credit_exhausted() -> httpx.Response:
    """Anthropic reports a dead balance as 400 + credit-balance text (ADR-074 credential reason)."""
    return httpx.Response(
        400,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API.",
            },
        },
    )


def chat_text(text_value: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-ok",
            "object": "chat.completion",
            "created": 0,
            "model": GPT,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text_value},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def chat_tool_call(call_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-tool",
            "object": "chat.completion",
            "created": 0,
            "model": GPT,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Reading the file.",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": "files_read", "arguments": '{"path":"a.txt"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def openai_unauthorized() -> httpx.Response:
    return httpx.Response(
        401,
        json={
            "error": {
                "message": "Incorrect API key provided.",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )


def responses_text(text_value: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_ok",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": GPT,
            "output": [
                {
                    "type": "message",
                    "id": "msg_ok",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text_value, "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    )


def responses_function_call(call_id: str, name: str, arguments: str) -> httpx.Response:
    """A Responses turn with the extra output items the compact form persists (§A3.1).

    ``reasoning`` / ``web_search_call`` and a ``text`` with ``annotations`` are exactly the blocks
    another provider does not define; the ``function_call`` becomes a compact ``tool_use``.
    """
    return httpx.Response(
        200,
        json={
            "id": "resp_tool",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": GPT,
            "output": [
                {"type": "reasoning", "id": "rs_1", "summary": []},
                {
                    "type": "web_search_call",
                    "id": "ws_1",
                    "status": "completed",
                    "action": {"type": "search", "query": "q"},
                },
                {
                    "type": "message",
                    "id": "msg_1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Reading the file.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com",
                                    "title": "t",
                                    "start_index": 0,
                                    "end_index": 4,
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "status": "completed",
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    )


_DEFAULT = {"anthropic": anthropic_text, "chat": chat_text, "responses": responses_text}


def _walk(obj: Any) -> Iterator[dict[str, Any]]:
    """Every dict nested anywhere in a request body."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _typed(body: dict[str, Any], block_type: str) -> list[dict[str, Any]]:
    return [d for d in _walk(body) if d.get("type") == block_type]


def _last_user_content(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Content list of the last user message/item of an outgoing body (``messages``/``input``)."""
    for item in reversed(body[key]):
        if item.get("role") == "user":
            content = item.get("content")
            assert isinstance(content, list), item
            return content
    raise AssertionError("no user message in the outgoing body")


# ============================ fixtures ============================


@pytest.fixture
def upstream(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> _Upstream:
    """REAL clients whose SDKs talk to ``_Upstream`` instead of the network.

    Depends on ``client`` so it runs after the conftest fixture has installed its fakes, and
    replaces all three process-wide singletons the orchestrator resolves at request time.
    """
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
    """The cached Settings with a hermetic two-provider baseline; every field restored after."""
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


def _claude_session_crossing_to_openai(s: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anthropic instance, Claude session, crossover model set — path 1, Anthropic → OpenAI."""
    monkeypatch.setattr(s, "llm_provider", "anthropic")
    monkeypatch.setattr(s, "anthropic_chat_fallback_openai_model", GPT)


def _gpt_session_crossing_to_anthropic(s: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI instance, GPT session, crossover model set — path 1, OpenAI → Anthropic."""
    monkeypatch.setattr(s, "llm_provider", "openai")
    monkeypatch.setattr(s, "openai_chat_fallback_anthropic_model", CLAUDE)


async def _credits_user(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with maker() as s:
        return await seed_user(s, subscription="active", balance=50)


async def _byok_user(maker: async_sessionmaker[AsyncSession], provider: str) -> uuid.UUID:
    async with maker() as s:
        uid = await seed_user(
            s, subscription="active", balance=0, byok_enabled=True, byok_status="valid"
        )
    await _set_byok_provider(maker, uid, provider)
    return uid


async def _set_byok_provider(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, provider: str
) -> None:
    async with maker() as s:
        await s.execute(
            text("UPDATE byok_keys SET provider=:p WHERE user_id=:u"),
            {"p": provider, "u": str(uid)},
        )
        await s.commit()


async def _run(
    client: AsyncClient,
    uid: uuid.UUID,
    *,
    v2: bool,
    attachments: list[dict[str, str]] | None = None,
    mode: str = "credits",
    message: str = "what is this?",
    workspace_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"userId": str(uid), "message": message, "mode": mode}
    if attachments:
        body["attachments"] = attachments
    if workspace_id is not None:
        body["workspaceProjectId"] = workspace_id
    if v2:
        body["generationMode"] = "general"
    r = await client.post(
        "/v1/chat/v2/run" if v2 else "/v1/chat/run", json=body, headers=auth_headers(uid)
    )
    assert r.status_code == 200, r.text
    payload: dict[str, Any] = r.json()
    return payload


async def _tool_result(
    client: AsyncClient, uid: uuid.UUID, run_body: dict[str, Any], *, v2: bool
) -> dict[str, Any]:
    r = await client.post(
        "/v1/chat/v2/tool-result" if v2 else "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": run_body["sessionId"],
            "toolCallId": run_body["toolCall"]["id"],
            "result": {"content": "file body"},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    payload: dict[str, Any] = r.json()
    return payload


async def _workspace_with_image(client: AsyncClient, uid: uuid.UUID) -> str:
    r = await client.post("/v1/workspaces", json={"name": "Proj"}, headers=auth_headers(uid))
    assert r.status_code == 201, r.text
    wid = str(r.json()["id"])
    up = await client.post(
        f"/v1/workspaces/{wid}/files",
        json={"type": "image", "mediaType": "image/png", "filename": "ws.png", "data": _PNG_B64},
        headers=auth_headers(uid),
    )
    assert up.status_code == 201, up.text
    return wid


# ==================== §A2 — attachments, every path / direction / receiver ====================


@pytest.mark.asyncio
async def test_row1_claude_failover_to_responses_carries_input_image(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row 1 — the NAMED prod defect: Claude session, Anthropic 400 credit → Responses (v2)."""
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted()]
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=True, attachments=[_png_attachment()])

    assert upstream.kinds() == ["anthropic", "responses"]
    body = upstream.bodies("responses")[0]
    parts = _last_user_content(body, "input")
    assert {"type": "input_image", "image_url": _PNG_URI, "detail": "auto"} in parts
    assert _typed(body, "image") == []


@pytest.mark.asyncio
async def test_row2_claude_failover_to_chat_completions_carries_image_url(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row 2: same failover on legacy ``/v1/chat/run`` (web search off) → Chat Completions."""
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted()]
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=False, attachments=[_png_attachment()])

    assert upstream.kinds() == ["anthropic", "chat"]
    body = upstream.bodies("chat")[0]
    parts = _last_user_content(body, "messages")
    assert {"type": "image_url", "image_url": {"url": _PNG_URI}} in parts
    assert _typed(body, "image") == []


@pytest.mark.asyncio
async def test_row3_gpt_failover_to_anthropic_carries_image_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row 3 — reverse direction: GPT session, OpenAI 401 on every key → AnthropicClient."""
    _gpt_session_crossing_to_anthropic(cfg, monkeypatch)
    upstream.scripts["chat"] = [openai_unauthorized()]
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=False, attachments=[_png_attachment()])

    assert upstream.kinds() == ["chat", "anthropic"]
    body = upstream.bodies("anthropic")[0]
    parts = _last_user_content(body, "messages")
    assert {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
    } in parts
    assert _typed(body, "image_url") == []


@pytest.mark.asyncio
async def test_row4_byok_anthropic_key_on_openai_instance_gets_image_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row 4 — path 2 without any provider failure: ``LLM_PROVIDER=openai``, Anthropic BYOK key."""
    monkeypatch.setattr(cfg, "llm_provider", "openai")
    uid = await _byok_user(db_sessionmaker, "anthropic")

    await _run(client, uid, v2=False, mode="byok", attachments=[_png_attachment()])

    assert upstream.kinds() == ["anthropic"]
    body = upstream.bodies("anthropic")[0]
    image = _typed(body, "image")
    assert image == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64}}
    ]
    assert _typed(body, "image_url") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("v2", "kind"), [(False, "chat"), (True, "responses")])
async def test_row5_byok_openai_key_on_anthropic_instance_gets_openai_form(
    v2: bool,
    kind: str,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row 5: ``LLM_PROVIDER=anthropic``, OpenAI BYOK key → both OpenAI endpoints."""
    monkeypatch.setattr(cfg, "llm_provider", "anthropic")
    uid = await _byok_user(db_sessionmaker, "openai")

    await _run(client, uid, v2=v2, mode="byok", attachments=[_png_attachment()])

    assert upstream.kinds() == [kind]
    body = upstream.bodies(kind)[0]
    if kind == "chat":
        assert {"type": "image_url", "image_url": {"url": _PNG_URI}} in _last_user_content(
            body, "messages"
        )
    else:
        assert {"type": "input_image", "image_url": _PNG_URI, "detail": "auto"} in (
            _last_user_content(body, "input")
        )
    assert _typed(body, "image") == []


@pytest.mark.asyncio
async def test_row6_operator_default_claude_on_openai_instance_gets_image_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row 6 — path 3: dual credits, ``LLM_PROVIDER=openai``, panel default model is Claude."""
    from app.instance_config.snapshot import refresh_snapshot
    from app.models import AdminSetting

    monkeypatch.setattr(cfg, "llm_provider", "openai")
    monkeypatch.setattr(cfg, "llm_providers_raw", "openai,anthropic")
    async with db_sessionmaker() as s:
        s.add(AdminSetting(setting_id="chat.default_model", value=CLAUDE))
        await s.commit()
        assert await refresh_snapshot(s, cfg)
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=False, attachments=[_png_attachment()])

    assert upstream.kinds() == ["anthropic"]
    body = upstream.bodies("anthropic")[0]
    assert body["model"] == CLAUDE
    assert {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
    } in _last_user_content(body, "messages")
    assert _typed(body, "image_url") == []


# ------------------------------- PDF: the same rows with `document` -------------------------------


@pytest.mark.asyncio
async def test_pdf_row1_claude_failover_to_responses_carries_input_file(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted()]
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=True, attachments=[_pdf_attachment()])

    body = upstream.bodies("responses")[0]
    assert {"type": "input_file", "filename": "d.pdf", "file_data": _PDF_URI} in (
        _last_user_content(body, "input")
    )
    assert _typed(body, "document") == []


@pytest.mark.asyncio
async def test_pdf_row2_claude_failover_to_chat_completions_carries_file_part(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted()]
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=False, attachments=[_pdf_attachment()])

    body = upstream.bodies("chat")[0]
    assert {"type": "file", "file": {"filename": "d.pdf", "file_data": _PDF_URI}} in (
        _last_user_content(body, "messages")
    )
    assert _typed(body, "document") == []


@pytest.mark.asyncio
async def test_pdf_row3_gpt_failover_to_anthropic_carries_document_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gpt_session_crossing_to_anthropic(cfg, monkeypatch)
    upstream.scripts["chat"] = [openai_unauthorized()]
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=False, attachments=[_pdf_attachment()])

    body = upstream.bodies("anthropic")[0]
    assert {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": _PDF_B64},
    } in _last_user_content(body, "messages")
    assert _typed(body, "file") == []


@pytest.mark.asyncio
async def test_pdf_row4_byok_anthropic_key_on_openai_instance_gets_document_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "llm_provider", "openai")
    uid = await _byok_user(db_sessionmaker, "anthropic")

    await _run(client, uid, v2=False, mode="byok", attachments=[_pdf_attachment()])

    body = upstream.bodies("anthropic")[0]
    assert len(_typed(body, "document")) == 1
    assert _typed(body, "file") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("v2", "kind"), [(False, "chat"), (True, "responses")])
async def test_pdf_row5_byok_openai_key_on_anthropic_instance_gets_openai_form(
    v2: bool,
    kind: str,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "llm_provider", "anthropic")
    uid = await _byok_user(db_sessionmaker, "openai")

    await _run(client, uid, v2=v2, mode="byok", attachments=[_pdf_attachment()])

    body = upstream.bodies(kind)[0]
    if kind == "chat":
        assert {"type": "file", "file": {"filename": "d.pdf", "file_data": _PDF_URI}} in (
            _last_user_content(body, "messages")
        )
    else:
        assert {"type": "input_file", "filename": "d.pdf", "file_data": _PDF_URI} in (
            _last_user_content(body, "input")
        )
    assert _typed(body, "document") == []


@pytest.mark.asyncio
async def test_pdf_row6_operator_default_claude_on_openai_instance_gets_document_block(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.instance_config.snapshot import refresh_snapshot
    from app.models import AdminSetting

    monkeypatch.setattr(cfg, "llm_provider", "openai")
    monkeypatch.setattr(cfg, "llm_providers_raw", "openai,anthropic")
    async with db_sessionmaker() as s:
        s.add(AdminSetting(setting_id="chat.default_model", value=CLAUDE))
        await s.commit()
        assert await refresh_snapshot(s, cfg)
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=False, attachments=[_pdf_attachment()])

    assert upstream.kinds() == ["anthropic"]
    body = upstream.bodies("anthropic")[0]
    assert {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": _PDF_B64},
    } in _last_user_content(body, "messages")
    assert _typed(body, "file") == []


# ------------------------------- workspace knowledge files -------------------------------


@pytest.mark.asyncio
async def test_workspace_image_on_claude_failover_to_responses(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project image instead of an inline attachment — the second producer (ADR-036 §6)."""
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted()]
    uid = await _credits_user(db_sessionmaker)
    wid = await _workspace_with_image(client, uid)

    await _run(client, uid, v2=True, workspace_id=wid)

    body = upstream.bodies("responses")[0]
    assert {"type": "input_image", "image_url": _PNG_URI, "detail": "auto"} in (
        _last_user_content(body, "input")
    )
    assert _typed(body, "image") == []


@pytest.mark.asyncio
async def test_workspace_image_on_gpt_failover_to_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gpt_session_crossing_to_anthropic(cfg, monkeypatch)
    upstream.scripts["chat"] = [openai_unauthorized()]
    uid = await _credits_user(db_sessionmaker)
    wid = await _workspace_with_image(client, uid)

    await _run(client, uid, v2=False, workspace_id=wid)

    body = upstream.bodies("anthropic")[0]
    assert {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
    } in _last_user_content(body, "messages")
    assert _typed(body, "image_url") == []


@pytest.mark.asyncio
async def test_workspace_image_byok_anthropic_key_on_openai_instance(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "llm_provider", "openai")
    uid = await _byok_user(db_sessionmaker, "anthropic")
    wid = await _workspace_with_image(client, uid)

    await _run(client, uid, v2=False, mode="byok", workspace_id=wid)

    body = upstream.bodies("anthropic")[0]
    assert len(_typed(body, "image")) == 1
    assert _typed(body, "image_url") == []


@pytest.mark.asyncio
async def test_workspace_image_byok_openai_key_on_anthropic_instance(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "llm_provider", "anthropic")
    uid = await _byok_user(db_sessionmaker, "openai")
    wid = await _workspace_with_image(client, uid)

    await _run(client, uid, v2=False, mode="byok", workspace_id=wid)

    body = upstream.bodies("chat")[0]
    assert {"type": "image_url", "image_url": {"url": _PNG_URI}} in _last_user_content(
        body, "messages"
    )
    assert _typed(body, "image") == []


# ------------------------- против переоценки: без смены провайдера -------------------------


def _placeholder(media_type: str, name: str, size: int) -> str:
    return f'[attachment: {media_type} "{name}", {size}B — отправлено в первом обращении к модели]'


async def _workspace_with_text_and_image(client: AsyncClient, uid: uuid.UUID) -> str:
    r = await client.post("/v1/workspaces", json={"name": "Proj"}, headers=auth_headers(uid))
    assert r.status_code == 201, r.text
    wid = str(r.json()["id"])
    for body in (
        {
            "type": "text",
            "mediaType": "text/plain",
            "filename": "notes.txt",
            "data": _b64(b"project notes"),
        },
        {"type": "image", "mediaType": "image/png", "filename": "ws.png", "data": _PNG_B64},
    ):
        up = await client.post(f"/v1/workspaces/{wid}/files", json=body, headers=auth_headers(uid))
        assert up.status_code == 201, up.text
    return wid


_MESSAGE = "look at these"
_PH_IMAGE = _placeholder("image/png", "p.png", len(_PNG))
_PH_TEXT = _placeholder("text/plain", "n.txt", len(_TEXT_FILE.encode("utf-8")))
_WS_TEXT = "[Файл проекта: notes.txt]\nproject notes"
_FILE_TEXT = f"n.txt\n```\n{_TEXT_FILE}\n```"


@pytest.mark.asyncio
async def test_no_failover_anthropic_body_equals_etalon(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
) -> None:
    """Без смены провайдера тело совпадает с эталоном, снятым ДО реализации (двойной рендер,
    лишние части, порядок «файлы проекта → вложения хода», одинаковый text-part)."""
    uid = await _credits_user(db_sessionmaker)
    wid = await _workspace_with_text_and_image(client, uid)

    await _run(
        client,
        uid,
        v2=False,
        message=_MESSAGE,
        workspace_id=wid,
        attachments=[_png_attachment(), _text_attachment()],
    )

    assert upstream.kinds() == ["anthropic"]
    assert upstream.bodies("anthropic")[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _MESSAGE},
                {"type": "text", "text": _PH_IMAGE},
                {"type": "text", "text": _PH_TEXT},
                {"type": "text", "text": _WS_TEXT},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
                },
                {"type": "text", "text": _FILE_TEXT},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_no_failover_chat_completions_body_equals_etalon(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "llm_provider", "openai")
    uid = await _credits_user(db_sessionmaker)
    wid = await _workspace_with_text_and_image(client, uid)

    await _run(
        client,
        uid,
        v2=False,
        message=_MESSAGE,
        workspace_id=wid,
        attachments=[_png_attachment(), _text_attachment()],
    )

    assert upstream.kinds() == ["chat"]
    messages = upstream.bodies("chat")[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1:] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{_MESSAGE}\n{_PH_IMAGE}\n{_PH_TEXT}"},
                {"type": "text", "text": _WS_TEXT},
                {"type": "image_url", "image_url": {"url": _PNG_URI}},
                {"type": "image_url", "image_url": {"url": _PNG_URI}},
                {"type": "text", "text": _FILE_TEXT},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_no_failover_responses_body_equals_etalon(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "llm_provider", "openai")
    uid = await _credits_user(db_sessionmaker)
    wid = await _workspace_with_text_and_image(client, uid)

    await _run(
        client,
        uid,
        v2=True,
        message=_MESSAGE,
        workspace_id=wid,
        attachments=[_png_attachment(), _text_attachment()],
    )

    assert upstream.kinds() == ["responses"]
    assert upstream.bodies("responses")[0]["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": _MESSAGE},
                {"type": "input_text", "text": _PH_IMAGE},
                {"type": "input_text", "text": _PH_TEXT},
                {"type": "input_text", "text": _WS_TEXT},
                {"type": "input_image", "image_url": _PNG_URI, "detail": "auto"},
                {"type": "input_image", "image_url": _PNG_URI, "detail": "auto"},
                {"type": "input_text", "text": _FILE_TEXT},
            ],
        }
    ]


# ------------------------------- each attempt — one set -------------------------------


@pytest.mark.asyncio
async def test_each_attempt_carries_exactly_one_set_of_parts(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two attempts (the first failed): each carries exactly as many parts as attachments sent."""
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted()]
    uid = await _credits_user(db_sessionmaker)

    await _run(client, uid, v2=False, attachments=[_png_attachment(), _pdf_attachment()])

    first = upstream.bodies("anthropic")[0]
    second = upstream.bodies("chat")[0]
    assert len(_typed(first, "image")) + len(_typed(first, "document")) == 2
    assert len(_typed(second, "image_url")) + len(_typed(second, "file")) == 2
    assert _typed(second, "image") == [] and _typed(second, "document") == []


# ------------------------- ADR-088: only the first round carries parts -------------------------


@pytest.mark.asyncio
async def test_next_tool_loop_round_after_failover_carries_only_the_placeholder(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the failover attempt answers with a server-side tool, the next round of the SAME
    call replays the history: the picture is a placeholder there, never the full part again."""
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted(), anthropic_credit_exhausted()]
    upstream.scripts["responses"] = [
        responses_function_call("call_time", "time_now", "{}"),
        responses_text("done"),
    ]
    uid = await _credits_user(db_sessionmaker)

    out = await _run(client, uid, v2=True, attachments=[_png_attachment()])

    assert out["status"] == "assistant_message", out
    first, second = upstream.bodies("responses")
    assert len(_typed(first, "input_image")) == 1
    assert _typed(second, "input_image") == []
    placeholder = {"type": "input_text", "text": _PH_IMAGE}
    assert placeholder in _last_user_content(second, "input")


# ==================== §A3 — history of any form, replayed by every client ====================


def _assert_pairing_chat(messages: list[dict[str, Any]]) -> None:
    """Chat Completions: every ``role=tool`` answers a call replayed EARLIER in the history."""
    called: set[str] = set()
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            called.add(call["id"])
        if msg.get("role") == "tool":
            assert msg["tool_call_id"] in called, messages
    answered = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    assert called == answered, messages


def _assert_pairing_anthropic(messages: list[dict[str, Any]]) -> None:
    called: set[str] = set()
    answered: set[str] = set()
    for msg in messages:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                called.add(block["id"])
            if block.get("type") == "tool_result":
                assert block["tool_use_id"] in called, messages
                answered.add(block["tool_use_id"])
    assert called == answered, messages


def _assert_pairing_responses(items: list[dict[str, Any]]) -> None:
    called: set[str] = set()
    answered: set[str] = set()
    for item in items:
        if item.get("type") == "function_call":
            called.add(item["call_id"])
        if item.get("type") == "function_call_output":
            assert item["call_id"] in called, items
            answered.add(item["call_id"])
    assert called == answered, items


@pytest.mark.asyncio
async def test_anthropic_steps_with_thinking_replayed_by_chat_completions(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic ``thinking``+``text``+``tool_use`` step + tool step → ``OpenAIClient``.

    Falls on the former ``_anthropic_blocks_to_openai_content`` (the call was dropped, the
    ``role=tool`` result stayed — a request Chat Completions rejects by schema).
    """
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [
        anthropic_tool_use("toolu_A1", thinking=True),
        anthropic_credit_exhausted(),
    ]
    uid = await _credits_user(db_sessionmaker)

    run = await _run(client, uid, v2=False, message="read a.txt")
    assert run["status"] == "tool_call", run
    await _tool_result(client, uid, run, v2=False)

    messages = upstream.bodies("chat")[0]["messages"]
    assistant = next(m for m in messages if m.get("role") == "assistant")
    assert assistant["content"] == "Reading the file."
    assert assistant["tool_calls"] == [
        {
            "id": "toolu_A1",
            "type": "function",
            "function": {"name": "files_read", "arguments": json.dumps({"path": "a.txt"})},
        }
    ]
    tool = next(m for m in messages if m.get("role") == "tool")
    assert tool["tool_call_id"] == "toolu_A1"
    assert "thinking" not in json.dumps(messages)
    _assert_pairing_chat(messages)


@pytest.mark.asyncio
async def test_chat_completions_step_replayed_by_anthropic(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat-Completions ``{role:"assistant", content, tool_calls}`` + tool step → Anthropic."""
    _gpt_session_crossing_to_anthropic(cfg, monkeypatch)
    upstream.scripts["chat"] = [chat_tool_call("call_C1"), openai_unauthorized()]
    uid = await _credits_user(db_sessionmaker)

    run = await _run(client, uid, v2=False, message="read a.txt")
    assert run["status"] == "tool_call", run
    await _tool_result(client, uid, run, v2=False)

    messages = upstream.bodies("anthropic")[0]["messages"]
    assistant = next(m for m in messages if m.get("role") == "assistant")
    assert assistant["content"] == [
        {"type": "text", "text": "Reading the file."},
        {"type": "tool_use", "id": "call_C1", "name": "files_read", "input": {"path": "a.txt"}},
    ]
    assert all("role" not in block for block in assistant["content"])
    results = [b for m in messages for b in (m.get("content") or []) if isinstance(b, dict)]
    assert any(b.get("type") == "tool_result" and b["tool_use_id"] == "call_C1" for b in results)
    _assert_pairing_anthropic(messages)


@pytest.mark.asyncio
async def test_responses_step_replayed_by_anthropic_drops_foreign_blocks(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Responses step (``text``+``annotations``, ``tool_use``, ``reasoning``, ``web_search_call``)
    → Anthropic: annotations stripped, reasoning/web_search_call dropped, tool_use kept."""
    _gpt_session_crossing_to_anthropic(cfg, monkeypatch)
    upstream.scripts["responses"] = [
        responses_function_call("call_R1", "files_read", '{"path": "a.txt"}'),
        openai_unauthorized(),
    ]
    uid = await _credits_user(db_sessionmaker)

    run = await _run(client, uid, v2=True, message="read a.txt")
    assert run["status"] == "tool_call", run
    await _tool_result(client, uid, run, v2=True)

    messages = upstream.bodies("anthropic")[0]["messages"]
    assistant = next(m for m in messages if m.get("role") == "assistant")
    assert assistant["content"] == [
        {"type": "text", "text": "Reading the file."},
        {"type": "tool_use", "id": "call_R1", "name": "files_read", "input": {"path": "a.txt"}},
    ]
    serialized = json.dumps(messages)
    assert "annotations" not in serialized
    assert "reasoning" not in serialized
    assert "web_search_call" not in serialized
    _assert_pairing_anthropic(messages)


async def _mixed_history_session(
    client: AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """A Claude session whose history holds an Anthropic round AND a Chat-Completions round.

    Round 1 — Anthropic answers with a tool call; round 2 (continuation) — Anthropic's balance is
    dead, OpenAI answers with ANOTHER tool call. Both steps are written by real clients.
    """
    _claude_session_crossing_to_openai(cfg, monkeypatch)
    upstream.scripts["anthropic"] = [
        anthropic_tool_use("toolu_M1"),
        anthropic_credit_exhausted(),
    ]
    upstream.scripts["chat"] = [chat_tool_call("call_M2")]
    uid = await _credits_user(maker)
    run = await _run(client, uid, v2=False, message="read a.txt")
    second = await _tool_result(client, uid, run, v2=False)
    assert second["status"] == "tool_call", second
    return uid, second


@pytest.mark.asyncio
async def test_mixed_history_pairing_holds_for_anthropic_reader(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic recovered: its client replays both rounds with every call ⇔ result paired."""
    uid, second = await _mixed_history_session(client, db_sessionmaker, upstream, cfg, monkeypatch)
    upstream.calls.clear()
    await _tool_result(client, uid, second, v2=False)

    assert upstream.kinds() == ["anthropic"]
    messages = upstream.bodies("anthropic")[0]["messages"]
    _assert_pairing_anthropic(messages)
    ids = [
        b["id"] for m in messages for b in (m.get("content") or []) if b.get("type") == "tool_use"
    ]
    assert ids == ["toolu_M1", "call_M2"]
    assert all("role" not in b for m in messages for b in (m.get("content") or []))


@pytest.mark.asyncio
async def test_mixed_history_pairing_holds_for_chat_completions_reader(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, second = await _mixed_history_session(client, db_sessionmaker, upstream, cfg, monkeypatch)
    upstream.calls.clear()
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted()]
    await _tool_result(client, uid, second, v2=False)

    assert upstream.kinds() == ["anthropic", "chat"]
    messages = upstream.bodies("chat")[0]["messages"]
    _assert_pairing_chat(messages)
    ids = [c["id"] for m in messages for c in (m.get("tool_calls") or [])]
    assert ids == ["toolu_M1", "call_M2"]


@pytest.mark.asyncio
async def test_mixed_history_pairing_holds_for_responses_reader(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid, second = await _mixed_history_session(client, db_sessionmaker, upstream, cfg, monkeypatch)
    upstream.calls.clear()
    upstream.scripts["anthropic"] = [anthropic_credit_exhausted()]
    # The legacy route replays through the Responses client when the instance opted into it.
    monkeypatch.setattr(cfg, "chat_legacy_web_search_enabled", True)
    await _tool_result(client, uid, second, v2=False)

    assert upstream.kinds() == ["anthropic", "responses"]
    items = upstream.bodies("responses")[0]["input"]
    _assert_pairing_responses(items)
    ids = [i["call_id"] for i in items if i.get("type") == "function_call"]
    assert ids == ["toolu_M1", "call_M2"]


@pytest.mark.asyncio
async def test_byok_key_replaced_with_openai_key_continues_with_valid_pairs(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    cfg: Settings,
) -> None:
    """A session started with an Anthropic BYOK key continues after the key is replaced with an
    OpenAI key: the OpenAI client builds a valid history (calls and results paired)."""
    uid = await _byok_user(db_sessionmaker, "anthropic")
    upstream.scripts["anthropic"] = [anthropic_tool_use("toolu_B1")]
    run = await _run(client, uid, v2=False, mode="byok", message="read a.txt")
    assert run["status"] == "tool_call", run

    await _set_byok_provider(db_sessionmaker, uid, "openai")
    await _tool_result(client, uid, run, v2=False)

    assert upstream.kinds() == ["anthropic", "chat"]
    messages = upstream.bodies("chat")[0]["messages"]
    _assert_pairing_chat(messages)
    assert [c["id"] for m in messages for c in (m.get("tool_calls") or [])] == ["toolu_B1"]
