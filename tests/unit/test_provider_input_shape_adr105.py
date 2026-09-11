"""Unit: рендер вложений тотален и живёт в клиенте; история своей формы — бит-в-бит (ADR-105 §A).

Норма — ``docs/modules/chat-orchestrator/09-testing.md`` §«Integration — вход вызова в форме
провайдера клиента (ADR-105)»: тотальность рендера (параметризация класс × клиент), «против
переоценки» для истории своей формы и ``thinking`` с обеих сторон.

Все три клиента — НАСТОЯЩИЕ классы; их SDK подменён только по транспорту (``httpx.MockTransport``),
ассерт идёт по телу исходящего HTTP-запроса. Шаги истории — ``LLMResult.content_blocks``, которые
сам реальный клиент построил из ответа провайдера (то, что оркестратор кладёт в ``chat_steps``).
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx
import openai
import pytest

from app.chat.anthropic_client import AnthropicClient
from app.chat.llm_client import NeutralMessage
from app.chat.openai_client import OpenAIClient
from app.chat.openai_responses_client import OpenAIResponsesClient
from app.config import get_settings
from app.schemas.chat import AttachmentIn

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _pdf_b64() -> str:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return _b64(buf.getvalue())


_ATTACHMENTS: dict[str, AttachmentIn] = {
    "image": AttachmentIn(type="image", mediaType="image/png", filename="p.png", data=_b64(_PNG)),
    "document": AttachmentIn(
        type="document", mediaType="application/pdf", filename="d.pdf", data=_pdf_b64()
    ),
    "text": AttachmentIn(type="text", mediaType="text/plain", filename="n.txt", data=_b64(b"hi")),
}

# Wire part types each client emits for the attachment parts it renders (ADR-033 §5, ADR-041).
_ATTACHMENT_PART_TYPES: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"image", "document", "text"}),
    "chat": frozenset({"image_url", "file", "text"}),
    "responses": frozenset({"input_image", "input_file", "input_text"}),
}


class _Wire:
    def __init__(self, response: Callable[[], httpx.Response]) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._response = response

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self._response()


def _anthropic_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _chat_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _responses_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "r",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "gpt-4o",
            "output": [
                {
                    "type": "message",
                    "id": "m",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    )


def _client(kind: str, response: Callable[[], httpx.Response] | None = None) -> tuple[Any, _Wire]:
    """A REAL client of ``kind`` whose SDK sends to ``_Wire`` instead of the network."""
    default = {"anthropic": _anthropic_ok, "chat": _chat_ok, "responses": _responses_ok}[kind]
    wire = _Wire(response or default)
    http = httpx.AsyncClient(transport=httpx.MockTransport(wire.handle))
    client: Any
    if kind == "anthropic":
        client = AnthropicClient()
        client._client = anthropic.AsyncAnthropic(
            api_key="placeholder", max_retries=0, http_client=http
        )
    elif kind == "chat":
        client = OpenAIClient()
        client._client = openai.AsyncOpenAI(api_key="placeholder", max_retries=0, http_client=http)
    else:
        client = OpenAIResponsesClient()
        client._client = openai.AsyncOpenAI(api_key="placeholder", max_retries=0, http_client=http)
    return client, wire


def _last_user_content(kind: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    key = "input" if kind == "responses" else "messages"
    for item in reversed(body[key]):
        if item.get("role") == "user":
            content = item.get("content")
            assert isinstance(content, list), item
            return content
    raise AssertionError("no user message")


_USER = NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "question"}])


# ============================ totality: class × client ============================


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("attachment_class", ["image", "document", "text"])
async def test_every_accepted_class_renders_to_exactly_one_wire_part(
    kind: str, attachment_class: str
) -> None:
    """For each class ``prepare_attachments`` accepts (``audio`` never reaches assembly) and each
    of the three clients: the number of wire parts equals the number of attachments."""
    from app.chat.attachments import prepare_attachments

    prepared = prepare_attachments([_ATTACHMENTS[attachment_class]], get_settings())
    client, wire = _client(kind)

    await client.create_message(system_prompt="s", messages=[_USER], tools=[], attachments=prepared)

    content = _last_user_content(kind, wire.bodies[0])
    # The first part is the user's own text; everything after it is rendered attachments.
    rendered = content[1:]
    assert len(rendered) == 1, content
    assert rendered[0]["type"] in _ATTACHMENT_PART_TYPES[kind]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "chat", "responses"])
async def test_all_classes_together_render_one_part_each(kind: str) -> None:
    from app.chat.attachments import prepare_attachments

    atts = [_ATTACHMENTS["image"], _ATTACHMENTS["document"], _ATTACHMENTS["text"]]
    prepared = prepare_attachments(atts, get_settings())
    client, wire = _client(kind)

    await client.create_message(system_prompt="s", messages=[_USER], tools=[], attachments=prepared)

    rendered = _last_user_content(kind, wire.bodies[0])[1:]
    assert len(rendered) == len(atts)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "chat", "responses"])
async def test_part_of_unknown_class_raises_before_the_upstream_call(kind: str) -> None:
    """A part the client cannot render is an exception BEFORE the upstream call, not a skip."""
    from app.chat.attachments import (
        AttachmentPart,
        PreparedAttachments,
        UnrenderableAttachmentError,
    )

    prepared = PreparedAttachments(
        parts=[AttachmentPart(kind="audio", media_type="audio/mp4", filename="v.m4a", data="AAAA")],
        placeholders=[],
    )
    client, wire = _client(kind)

    with pytest.raises(UnrenderableAttachmentError):
        await client.create_message(
            system_prompt="s", messages=[_USER], tools=[], attachments=prepared
        )
    assert wire.bodies == []


# ============================ history of the client's OWN form ============================


def _anthropic_tool_turn() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_t",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [
                {"type": "thinking", "thinking": "let me look", "signature": "sig-1"},
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "toolu_1", "name": "files_read", "input": {"path": "a"}},
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _chat_tool_turn() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "files_read", "arguments": '{"path":"a"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _responses_tool_turn() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "r",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "gpt-4o",
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
                    "id": "m",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Reading.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com",
                                    "title": "t",
                                    "start_index": 0,
                                    "end_index": 3,
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "files_read",
                    "arguments": '{"path":"a"}',
                    "status": "completed",
                },
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    )


async def _persisted_step(kind: str) -> list[dict[str, Any]]:
    """``LLMResult.content_blocks`` the REAL client of ``kind`` builds for a tool-call turn."""
    turn = {"anthropic": _anthropic_tool_turn, "chat": _chat_tool_turn}.get(
        kind, _responses_tool_turn
    )
    client, _ = _client(kind, turn)
    result = await client.create_message(system_prompt="s", messages=[_USER], tools=[])
    blocks: list[dict[str, Any]] = json.loads(json.dumps(result.content_blocks))
    return blocks


def _history(step: list[dict[str, Any]], tool_id: str) -> list[NeutralMessage]:
    return [
        _USER,
        NeutralMessage(role="assistant", content_blocks=step),
        NeutralMessage(
            role="tool",
            tool_call_id="dom-1",
            provider_tool_use_id=tool_id,
            tool_name="files.read",
            result={"content": "body"},
        ),
    ]


@pytest.mark.asyncio
async def test_anthropic_own_form_history_replays_bit_for_bit() -> None:
    """Etalon taken BEFORE the change: the stored Anthropic blocks go back verbatim."""
    step = await _persisted_step("anthropic")
    client, wire = _client("anthropic")

    await client.create_message(system_prompt="s", messages=_history(step, "toolu_1"), tools=[])

    assert wire.bodies[0]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "question"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "let me look", "signature": "sig-1"},
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "toolu_1", "name": "files_read", "input": {"path": "a"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"content": "body"}',
                    "is_error": False,
                }
            ],
        },
    ]


@pytest.mark.asyncio
async def test_chat_completions_own_form_history_replays_bit_for_bit() -> None:
    step = await _persisted_step("chat")
    client, wire = _client("chat")

    await client.create_message(system_prompt="s", messages=_history(step, "call_1"), tools=[])

    assert wire.bodies[0]["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "files_read", "arguments": '{"path":"a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"content": "body"}'},
    ]


@pytest.mark.asyncio
async def test_responses_own_form_history_replays_bit_for_bit() -> None:
    step = await _persisted_step("responses")
    client, wire = _client("responses")

    await client.create_message(system_prompt="s", messages=_history(step, "call_1"), tools=[])

    assert wire.bodies[0]["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "question"}],
        },
        {"id": "rs_1", "summary": [], "type": "reasoning"},
        {"type": "message", "role": "assistant", "content": "Reading."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "files_read",
            "arguments": '{"path": "a"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"content": "body"}'},
    ]


# ============================ extended thinking — both sides ============================


@pytest.mark.asyncio
async def test_reasoning_without_thinking_when_last_step_before_tool_result_is_foreign() -> None:
    """Anthropic, ``reasoning``: the last assistant step before ``tool_result`` was answered by
    OpenAI — it has no thinking block and cannot have one — so the attempt goes WITHOUT thinking."""
    step = await _persisted_step("chat")
    client, wire = _client("anthropic")

    await client.create_message(
        system_prompt="s",
        messages=_history(step, "call_1"),
        tools=[],
        generation_mode="reasoning",
    )

    body = wire.bodies[0]
    assert "thinking" not in body
    assistant = body["messages"][1]
    assert assistant["content"][0]["type"] != "thinking"


@pytest.mark.asyncio
async def test_reasoning_keeps_thinking_when_own_step_starts_with_a_thinking_block() -> None:
    step = await _persisted_step("anthropic")
    client, wire = _client("anthropic")

    await client.create_message(
        system_prompt="s",
        messages=_history(step, "toolu_1"),
        tools=[],
        generation_mode="reasoning",
    )

    body = wire.bodies[0]
    assert body["thinking"]["type"] == "enabled"
    assert body["messages"][1]["content"][0]["type"] == "thinking"
