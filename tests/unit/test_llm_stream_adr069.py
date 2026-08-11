"""Unit: LLMClient.stream_message glue → LLMResult (ADR-069)."""

from __future__ import annotations

import pytest

from app.chat.llm_client import StreamEvent
from tests.conftest import FakeAnthropicClient


@pytest.mark.asyncio
async def test_fake_stream_message_emits_deltas_then_completed() -> None:
    fake = FakeAnthropicClient()
    fake.responses = [fake.text_result("Hello world!!")]
    fake.stream_chunks = [["Hel", "lo ", "world!!"]]

    events: list[StreamEvent] = []
    async for event in fake.stream_message(
        system_prompt="sys",
        messages=[],
        tools=[],
    ):
        events.append(event)

    assert [e.kind for e in events] == ["text_delta", "text_delta", "text_delta", "completed"]
    assert "".join(e.text for e in events if e.kind == "text_delta") == "Hello world!!"
    assert events[-1].result is not None
    assert events[-1].result.text == "Hello world!!"
    assert events[-1].result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_fake_stream_message_auto_chunks_text() -> None:
    fake = FakeAnthropicClient()
    fake.responses = [fake.text_result("abcdefghij")]  # 10 chars → 3 chunks

    deltas: list[str] = []
    completed = None
    async for event in fake.stream_message(system_prompt="s", messages=[], tools=[]):
        if event.kind == "text_delta":
            deltas.append(event.text)
        else:
            completed = event.result

    assert len(deltas) == 3
    assert "".join(deltas) == "abcdefghij"
    assert completed is not None
    assert completed.text == "abcdefghij"
