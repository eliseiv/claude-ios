"""Unit: per-instance CHAT_DISABLED_TOOL_FAMILIES (ADR-081)."""

from __future__ import annotations

import pytest

from app.chat.orchestrator import _SYSTEM_PROMPT_CHAT, _compose_system_prompt, _system_prompt_for
from app.chat.tools import (
    DISABLEABLE_TOOL_FAMILIES,
    TOOL_CALENDAR_READ,
    TOOL_FILES_READ,
    TOOL_MEDIA_ASK_PARAMS,
    TOOL_REMINDERS_READ,
    TOOL_SITE_PREVIEW,
    TOOL_TIME_NOW,
    neutral_tool_definitions,
    offered_tool_family,
    parse_disabled_tool_families,
    tool_catalog,
    tool_family,
)
from app.config import Settings, get_settings


def test_known_disableable_families() -> None:
    assert frozenset({"files", "calendar", "reminders", "site"}) == DISABLEABLE_TOOL_FAMILIES


def test_tool_family_prefix() -> None:
    assert tool_family("files.read") == "files"
    assert tool_family("calendar.create_events") == "calendar"
    assert tool_family("time.now") == "time"


def test_parse_empty_is_noop() -> None:
    assert parse_disabled_tool_families("") == (frozenset(), ())
    assert parse_disabled_tool_families("  ,  ") == (frozenset(), ())


def test_parse_accepts_novirell_list() -> None:
    accepted, unknown = parse_disabled_tool_families("files, calendar, reminders, site")
    assert accepted == DISABLEABLE_TOOL_FAMILIES
    assert unknown == ()


def test_parse_is_case_insensitive_and_skips_unknown() -> None:
    accepted, unknown = parse_disabled_tool_families("Files, MEDIA, nope")
    assert accepted == frozenset({"files"})
    assert unknown == ("media", "nope")


def test_offered_tool_family_gate() -> None:
    disabled = frozenset({"files", "site"})
    assert not offered_tool_family(TOOL_FILES_READ, disabled_families=disabled)
    assert not offered_tool_family(TOOL_SITE_PREVIEW, disabled_families=disabled)
    assert offered_tool_family(TOOL_CALENDAR_READ, disabled_families=disabled)
    assert offered_tool_family(TOOL_TIME_NOW, disabled_families=disabled)


def test_neutral_defs_drop_disabled_families_keep_others() -> None:
    names = {
        d["name"]
        for d in neutral_tool_definitions(
            disabled_families=frozenset({"files", "calendar", "reminders", "site"})
        )
    }
    assert TOOL_FILES_READ not in names
    assert TOOL_CALENDAR_READ not in names
    assert TOOL_REMINDERS_READ not in names
    assert TOOL_SITE_PREVIEW not in names
    assert TOOL_TIME_NOW in names
    assert TOOL_MEDIA_ASK_PARAMS in names


def test_catalog_default_stays_full() -> None:
    names = {t["name"] for t in tool_catalog()}
    assert TOOL_FILES_READ in names
    assert TOOL_SITE_PREVIEW in names


def test_catalog_filters_disabled_families() -> None:
    hidden = frozenset({"files", "calendar", "reminders", "site"})
    names = {t["name"] for t in tool_catalog(disabled_families=hidden)}
    assert TOOL_FILES_READ not in names
    assert TOOL_CALENDAR_READ not in names
    assert TOOL_REMINDERS_READ not in names
    assert TOOL_SITE_PREVIEW not in names
    assert TOOL_TIME_NOW in names


def test_settings_default_disables_nothing() -> None:
    get_settings.cache_clear()
    assert Settings().disabled_tool_families() == frozenset()  # type: ignore[call-arg]
    get_settings.cache_clear()


def test_settings_parses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_DISABLED_TOOL_FAMILIES", "files,calendar,reminders,site")
    get_settings.cache_clear()
    assert get_settings().disabled_tool_families() == DISABLEABLE_TOOL_FAMILIES
    get_settings.cache_clear()


def test_compose_prompt_empty_disabled_is_canonical() -> None:
    assert _compose_system_prompt("chat", frozenset()) == _SYSTEM_PROMPT_CHAT


def test_compose_prompt_omits_disabled_families(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_DISABLED_TOOL_FAMILIES", "files,calendar,reminders,site")
    get_settings.cache_clear()
    prompt = _system_prompt_for("chat")
    get_settings.cache_clear()
    assert "executes locally" not in prompt
    assert "site tools" not in prompt
