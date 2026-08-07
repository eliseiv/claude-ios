"""Unit coverage for the static `study_learn` system-prompt suffix (ADR-064 §7 soft level).

09-testing.md §Study & Learn → «Unit — системный суффикс режима». The wiring half of that section
(«доходит до провайдера») is deliberately NOT here: it is asserted on the working HTTP path in
tests/integration/test_study_learn_quiz_adr064.py, because a helper-level assertion cannot show
that the orchestrator actually hands the suffix to the client.
"""

from __future__ import annotations

import pytest

from app.chat.orchestrator import (
    _STUDY_LEARN_INSTRUCTION,
    _system_prompt_for,
    _system_prompt_with_workspace,
)

_ASSISTANT_MODES = ("chat", "code")


@pytest.mark.parametrize("assistant_mode", _ASSISTANT_MODES)
def test_suffix_present_only_in_study_learn(assistant_mode: str) -> None:
    # diff: fails if the suffix is appended unconditionally, and fails if it is never appended
    # (a declared-but-unwired instruction).
    assert _STUDY_LEARN_INSTRUCTION in _system_prompt_for(assistant_mode, "study_learn")
    for mode in ("general", "research", "reasoning"):
        assert _STUDY_LEARN_INSTRUCTION not in _system_prompt_for(assistant_mode, mode), mode
    # The legacy path calls the helper without a mode → the `general` default → no suffix.
    assert _STUDY_LEARN_INSTRUCTION not in _system_prompt_for(assistant_mode)


@pytest.mark.parametrize("assistant_mode", _ASSISTANT_MODES)
def test_study_learn_prompt_is_the_base_prompt_plus_the_suffix(assistant_mode: str) -> None:
    base = _system_prompt_for(assistant_mode, "general")
    composed = _system_prompt_for(assistant_mode, "study_learn")
    assert composed.startswith(base)
    assert composed.endswith(_STUDY_LEARN_INSTRUCTION)


def test_suffix_states_the_anti_spoiler_rules() -> None:
    text = _STUDY_LEARN_INSTRUCTION.lower()
    assert "quiz.generate" in text
    assert "never repeat the question wording" in text
    assert "never reveal the correct options" in text


def test_layer_order_base_then_suffix_then_workspace_instructions() -> None:
    # ADR-036 §3: the user's workspace instructions stay LAST, after the mode suffix.
    instructions = "Always answer in pirate speak."
    composed = _system_prompt_with_workspace("chat", instructions, "study_learn")
    base_tail = _system_prompt_for("chat", "general")[-40:]
    assert composed.index(base_tail) < composed.index(_STUDY_LEARN_INSTRUCTION)
    assert composed.index(_STUDY_LEARN_INSTRUCTION) < composed.index(instructions)
    assert composed.endswith(instructions)


def test_workspace_composition_without_the_mode_keeps_previous_behaviour() -> None:
    instructions = "Be terse."
    composed = _system_prompt_with_workspace("chat", instructions)
    assert _STUDY_LEARN_INSTRUCTION not in composed
    assert composed.endswith(instructions)


def test_suffix_is_static_so_the_prompt_prefix_stays_cacheable() -> None:
    # Same bytes on repeated reads: no date, no counters, no turn content (prompt-cache stability
    # inside the mode). The per-turn byte-equality on the real path is asserted in integration.
    assert _system_prompt_for("chat", "study_learn") == _system_prompt_for("chat", "study_learn")
    assert "{" not in _STUDY_LEARN_INSTRUCTION
    assert "%" not in _STUDY_LEARN_INSTRUCTION
