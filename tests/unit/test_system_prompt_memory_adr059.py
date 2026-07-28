"""ADR-059: the base system prompt asserts conversational memory in both modes.

Regression guard for the tester-reported bug where the model replied "I can't store/recall
information" despite the prior turns being replayed. The prompt must (a) tell the model it has the
conversation history and (b) forbid the statelessness disclaimer — in BOTH chat and code modes,
and it must remain static (no per-request interpolation) so the Anthropic cache prefix is stable.
"""

from __future__ import annotations

from app.chat.orchestrator import (
    _CONVERSATION_MEMORY_INSTRUCTION,
    _system_prompt_for,
    _system_prompt_with_workspace,
)


def test_both_modes_include_conversation_memory_instruction() -> None:
    assert _CONVERSATION_MEMORY_INSTRUCTION in _system_prompt_for("chat")
    assert _CONVERSATION_MEMORY_INSTRUCTION in _system_prompt_for("code")


def test_memory_instruction_forbids_statelessness_disclaimer() -> None:
    text = _CONVERSATION_MEMORY_INSTRUCTION.lower()
    assert "full history of the current conversation" in text
    assert "never claim you cannot remember" in text


def test_prompt_is_static_no_interpolation() -> None:
    # Same value on repeated reads (keeps the prompt-cache prefix stable, like _TIME_NOW).
    assert _system_prompt_for("chat") == _system_prompt_for("chat")
    assert "{" not in _CONVERSATION_MEMORY_INSTRUCTION


def test_workspace_instructions_still_appended_after_memory_line() -> None:
    base = _system_prompt_for("chat")
    composed = _system_prompt_with_workspace("chat", "Always answer in pirate speak.")
    # Memory line lives in the base; workspace instructions still graft after it (ADR-036 §3).
    assert _CONVERSATION_MEMORY_INSTRUCTION in composed
    assert composed.startswith(base)
    assert composed.endswith("Always answer in pirate speak.")
