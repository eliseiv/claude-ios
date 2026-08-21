"""Unit coverage for the static `research` system-prompt suffix (ADR-084).

Mirrors tests/unit/test_study_learn_prompt_adr064.py. Wiring («доходит до провайдера») is asserted
on the working HTTP path in tests/e2e/test_chat_flows.py and
tests/integration/test_legacy_web_search_adr082.py.
"""

from __future__ import annotations

import pytest

from app.chat.orchestrator import (
    _RESEARCH_INSTRUCTION,
    _STUDY_LEARN_INSTRUCTION,
    _system_prompt_for,
    _system_prompt_with_workspace,
)

_ASSISTANT_MODES = ("chat", "code")


@pytest.mark.parametrize("assistant_mode", _ASSISTANT_MODES)
def test_suffix_present_only_in_research(assistant_mode: str) -> None:
    assert _RESEARCH_INSTRUCTION in _system_prompt_for(assistant_mode, "research")
    for mode in ("general", "reasoning", "study_learn"):
        assert _RESEARCH_INSTRUCTION not in _system_prompt_for(assistant_mode, mode), mode
    assert _RESEARCH_INSTRUCTION not in _system_prompt_for(assistant_mode)
    assert _STUDY_LEARN_INSTRUCTION not in _system_prompt_for(assistant_mode, "research")


@pytest.mark.parametrize("assistant_mode", _ASSISTANT_MODES)
def test_research_prompt_is_the_base_prompt_plus_the_suffix(assistant_mode: str) -> None:
    base = _system_prompt_for(assistant_mode, "general")
    composed = _system_prompt_for(assistant_mode, "research")
    assert composed.startswith(base)
    assert composed.endswith(_RESEARCH_INSTRUCTION)


def test_suffix_states_live_search_and_forbids_dummy_queries() -> None:
    text = _RESEARCH_INSTRUCTION.lower()
    assert "research turn" in text
    assert "web-search" in text
    assert "public internet" in text
    assert "dummy" in text
    assert "calculator" in text
    assert "source links" in text
    assert "no internet" in text


def test_layer_order_base_then_suffix_then_workspace_instructions() -> None:
    instructions = "Always answer in pirate speak."
    composed = _system_prompt_with_workspace("chat", instructions, "research")
    base_tail = _system_prompt_for("chat", "general")[-40:]
    assert composed.index(base_tail) < composed.index(_RESEARCH_INSTRUCTION)
    assert composed.index(_RESEARCH_INSTRUCTION) < composed.index(instructions)
    assert composed.endswith(instructions)


def test_workspace_composition_without_the_mode_keeps_previous_behaviour() -> None:
    instructions = "Be terse."
    composed = _system_prompt_with_workspace("chat", instructions)
    assert _RESEARCH_INSTRUCTION not in composed
    assert composed.endswith(instructions)


def test_suffix_is_static_so_the_prompt_prefix_stays_cacheable() -> None:
    assert _system_prompt_for("chat", "research") == _system_prompt_for("chat", "research")
    assert "{" not in _RESEARCH_INSTRUCTION
    assert "%" not in _RESEARCH_INSTRUCTION
