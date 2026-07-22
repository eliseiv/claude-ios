"""ADR-058: provider-shaped assistant payload → domain blocks (pure adapter).

Covers both providers' persisted shapes plus the defensive branches (the adapter sits on a
read path and must never raise).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.chats.provider_blocks import to_domain_blocks


def test_anthropic_blocks_pass_through_unchanged() -> None:
    content: list[Any] = [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "toolu_1", "name": "site_write_file", "input": {"path": "a"}},
    ]
    assert to_domain_blocks(content) is content


def test_openai_assistant_message_text_becomes_text_block() -> None:
    content = [{"role": "assistant", "content": "проверка"}]
    assert to_domain_blocks(content) == [{"type": "text", "text": "проверка"}]


def test_openai_tool_calls_become_tool_use_blocks_with_raw_ids() -> None:
    content = [
        {
            "role": "assistant",
            "content": "работаю",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "site_write_file",
                        "arguments": '{"path": "index.html"}',
                    },
                }
            ],
        }
    ]
    assert to_domain_blocks(content) == [
        {"type": "text", "text": "работаю"},
        # Raw provider id + underscore name are preserved: ADR-024 normalization maps them later.
        {
            "type": "tool_use",
            "id": "call_abc",
            "name": "site_write_file",
            "input": {"path": "index.html"},
        },
    ]


def test_openai_tool_call_without_text_yields_only_tool_use() -> None:
    content = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "function": {"name": "time_now", "arguments": "{}"}},
            ],
        }
    ]
    assert to_domain_blocks(content) == [
        {"type": "tool_use", "id": "call_1", "name": "time_now", "input": {}}
    ]


@pytest.mark.parametrize("arguments", ["not json", "", "[1, 2]", 42, None])
def test_unparsable_tool_arguments_degrade_to_empty_input(arguments: Any) -> None:
    function: dict[str, Any] = {"name": "time_now", "arguments": arguments}
    content = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "function": function}],
        }
    ]
    assert to_domain_blocks(content) == [
        {"type": "tool_use", "id": "call_1", "name": "time_now", "input": {}}
    ]


@pytest.mark.parametrize(
    "content",
    [
        None,
        "text",
        {},
        [{"role": "assistant"}, {"role": "assistant"}],  # >1 element → not our shape
    ],
)
def test_non_convertible_content_is_safe(content: Any) -> None:
    result = to_domain_blocks(content)
    assert isinstance(result, list)


def test_user_step_blocks_are_never_touched() -> None:
    content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "[файл: a.pdf]"}]
    assert to_domain_blocks(content) == content


def test_openai_message_without_content_or_tool_calls_is_empty() -> None:
    assert to_domain_blocks([{"role": "assistant", "content": None}]) == []
