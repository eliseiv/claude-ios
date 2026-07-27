"""Unit tests for Anthropic generation-mode wire parameters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.chat.anthropic_client import AnthropicClient
from app.chat.llm_client import NeutralMessage
from app.chat.tools import neutral_tool_definitions
from app.config import get_settings


class _FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


class _FakeAnthropicSDK:
    def __init__(self) -> None:
        self.messages = _FakeMessages()
        self.options_key: str | None = None

    def with_options(self, *, api_key: str) -> _FakeAnthropicSDK:
        self.options_key = api_key
        return self


def _client_with_fake_sdk() -> tuple[AnthropicClient, _FakeAnthropicSDK]:
    client = AnthropicClient()
    fake = _FakeAnthropicSDK()
    client._client = fake  # type: ignore[assignment]
    return client, fake


@pytest.mark.asyncio
async def test_research_generation_mode_adds_anthropic_web_search_tool() -> None:
    client, fake = _client_with_fake_sdk()

    await client.create_message(
        system_prompt="s",
        messages=[NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "q"}])],
        tools=neutral_tool_definitions(include_server_side=False),
        generation_mode="research",
    )

    sent = fake.messages.calls[0]
    web_tool = sent["tools"][-1]
    assert web_tool == {
        "type": get_settings().anthropic_web_search_tool_type,
        "name": "web_search",
        "response_inclusion": "excluded",
    }
    assert sent["extra_body"] is None


@pytest.mark.asyncio
async def test_reasoning_generation_mode_sends_extended_thinking_body() -> None:
    client, fake = _client_with_fake_sdk()
    settings = get_settings()
    original_budget = settings.anthropic_thinking_budget_tokens
    original_display = settings.anthropic_thinking_display
    settings.anthropic_thinking_budget_tokens = 1234
    settings.anthropic_thinking_display = "summarized"
    try:
        await client.create_message(
            system_prompt="s",
            messages=[NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "q"}])],
            tools=[],
            generation_mode="reasoning",
        )
    finally:
        settings.anthropic_thinking_budget_tokens = original_budget
        settings.anthropic_thinking_display = original_display

    sent = fake.messages.calls[0]
    assert sent["extra_body"] == {
        "thinking": {"type": "enabled", "budget_tokens": 1234, "display": "summarized"}
    }
    assert sent["tools"] == []


def test_anthropic_usage_parses_thinking_and_web_search_counts() -> None:
    message = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=2,
            cache_creation_input_tokens=3,
            output_tokens_details=SimpleNamespace(thinking_tokens=4),
            server_tool_use=SimpleNamespace(web_search_requests=1),
        )
    )

    usage = AnthropicClient._parse_usage(message, "claude-sonnet-4-5")

    assert usage.reasoning_tokens == 4
    assert usage.web_search_requests == 1
    assert usage.cache_read_tokens == 2
    assert usage.cache_write_tokens == 3


@pytest.mark.asyncio
async def test_general_generation_mode_sends_plain_messages_call() -> None:
    client, fake = _client_with_fake_sdk()

    await client.create_message(
        system_prompt="s",
        messages=[NeutralMessage(role="user", content_blocks=[{"type": "text", "text": "q"}])],
        tools=[],
        generation_mode="general",
    )

    sent = fake.messages.calls[0]
    assert "extra_body" not in sent
    assert sent["tools"] == []
