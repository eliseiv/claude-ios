"""Read-boundary adapter: provider-shaped assistant payloads → domain content blocks (ADR-058).

``chat_steps.payload["content"]`` holds whatever the ACTIVE provider client returned as
``LLMResult.content_blocks`` — it must stay wire-valid for that provider's replay
(``_build_provider_messages``), so its shape is provider-specific (ADR-021/ADR-033 §3):

- ``anthropic``: domain-shaped blocks already — ``[{"type":"text",...}, {"type":"tool_use",...}]``;
- ``openai``: the normalized OpenAI assistant MESSAGE as a single element —
  ``[{"role":"assistant","content":"...","tool_calls":[...]}]`` (``openai_client`` §parse).

Every user-facing chats read (preview, history, steps-view) is specified on the DOMAIN block shape
(ADR-024), so on an OpenAI instance those readers found no ``type=="text"`` block and silently
degraded: ``GET /v1/chats`` returned ``preview: null``, ``GET /v1/chats/{id}`` leaked the raw
OpenAI message (including raw ``call_...`` ids, forbidden by ADR-008) and
``GET /v1/chats/{id}/steps`` emitted neither the assistant summary nor its ``tool_call`` entries.

This module converts at the SERIALIZATION BOUNDARY only — same discipline as ADR-024/ADR-042:
the stored payload is never mutated (replay/generation are untouched) and no migration is needed,
so chats written before the fix render correctly too. Anthropic-shaped content is returned as-is.
"""

from __future__ import annotations

import json
from typing import Any


def to_domain_blocks(content: Any) -> list[Any]:
    """Return ``content`` as domain blocks (``text`` / ``tool_use`` / …), converting if needed.

    Non-list input yields ``[]``. Anthropic-shaped content is returned UNCHANGED (same list
    object — callers that mutate must deep-copy first, as ``_normalize_payload`` does). Only the
    OpenAI assistant-message shape is converted, on a fresh list of fresh dicts.
    """
    if not isinstance(content, list):
        return []
    if len(content) == 1 and _is_openai_assistant_message(content[0]):
        return _from_openai_assistant_message(content[0])
    return content


def _is_openai_assistant_message(block: Any) -> bool:
    """True for our persisted OpenAI assistant message, never for a domain block.

    Domain blocks are discriminated by ``type`` and carry no ``role``; the OpenAI message is the
    mirror image (``role=="assistant"``, no ``type``).
    """
    return isinstance(block, dict) and block.get("role") == "assistant" and "type" not in block


def _from_openai_assistant_message(message: dict[str, Any]) -> list[Any]:
    """Map ``{role:assistant, content, tool_calls}`` → ``text`` + ``tool_use`` domain blocks.

    ``tool_use.id`` keeps the RAW provider id (``call_...``) and ``name`` the provider (underscore)
    name — exactly like an Anthropic ``tool_use`` block at this point. The downstream ADR-024
    normalization maps both to their domain values, so ADR-008 still holds (the raw id never
    reaches the response). Defensive throughout: this is a read path and must never raise.
    """
    blocks: list[Any] = []
    text = message.get("content")
    if isinstance(text, str):
        if text:
            blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        # Content-part list (not produced by our client today; tolerated for forward-compat).
        for part in text:
            if isinstance(part, dict) and part.get("type") == "text":
                part_text = part.get("text")
                if isinstance(part_text, str) and part_text:
                    blocks.append({"type": "text", "text": part_text})
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return blocks
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id"),
                "name": name,
                "input": _parse_arguments(function.get("arguments")),
            }
        )
    return blocks


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    """OpenAI ``function.arguments`` (a JSON STRING) → dict; anything unparsable → ``{}``."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}
