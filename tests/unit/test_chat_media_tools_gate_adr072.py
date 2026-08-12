"""Unit: CHAT_MEDIA_TOOLS_ENABLED instance gate (ADR-072)."""

from __future__ import annotations

import pytest

from app.chat.tools import (
    MEDIA_CHAT_TOOLS,
    TOOL_MEDIA_ASK_PARAMS,
    TOOL_MEDIA_GENERATE_IMAGE,
    TOOL_MEDIA_GENERATE_VIDEO,
    TOOL_TIME_NOW,
    neutral_tool_definitions,
    offered_media_chat_tool,
)
from app.config import Settings, get_settings


def test_media_chat_tools_set() -> None:
    assert TOOL_MEDIA_ASK_PARAMS in MEDIA_CHAT_TOOLS
    assert TOOL_MEDIA_GENERATE_IMAGE in MEDIA_CHAT_TOOLS
    assert TOOL_MEDIA_GENERATE_VIDEO in MEDIA_CHAT_TOOLS
    assert len(MEDIA_CHAT_TOOLS) == 3


def test_offered_media_chat_tool_gate() -> None:
    assert offered_media_chat_tool(TOOL_TIME_NOW, include_media_chat_tools=False)
    assert offered_media_chat_tool(TOOL_MEDIA_ASK_PARAMS, include_media_chat_tools=True)
    assert not offered_media_chat_tool(TOOL_MEDIA_ASK_PARAMS, include_media_chat_tools=False)


def test_neutral_defs_drop_media_when_disabled() -> None:
    names = {d["name"] for d in neutral_tool_definitions(include_media_chat_tools=False)}
    assert TOOL_TIME_NOW in names
    assert TOOL_MEDIA_ASK_PARAMS not in names
    assert TOOL_MEDIA_GENERATE_IMAGE not in names
    assert TOOL_MEDIA_GENERATE_VIDEO not in names


def test_settings_default_enables_chat_media_tools() -> None:
    get_settings.cache_clear()
    assert Settings().chat_media_tools_enabled is True  # type: ignore[call-arg]
    get_settings.cache_clear()


def test_settings_parses_false(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_MEDIA_TOOLS_ENABLED", "false")
    get_settings.cache_clear()
    assert get_settings().chat_media_tools_enabled is False
    get_settings.cache_clear()
