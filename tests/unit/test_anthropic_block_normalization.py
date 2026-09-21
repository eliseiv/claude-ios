"""Unit tests for ADR-021 content-block normalization (_normalize_block, scenario 3).

block.model_dump() from the Anthropic SDK carries non-wire fields (e.g. "caller":{"type":"direct"})
that are garbage on replay and break the payload-purity invariant. _normalize_block keeps ONLY the
wire-valid fields per block type (allowlist), preserving raw tool_use.id verbatim (ADR-008) and
never losing real content. Unknown block types must not blow up (forward-compatible).

These are pure-function tests (no I/O); the persist-boundary integration (payload + assembled
messages carry no `caller`) is covered in tests/integration/test_chat_tool_loop_seq.py.

Hosted web search (`generation_mode == "research"`) adds a second concern, covered at the bottom of
this file: the installed SDK models neither `server_tool_use` nor `web_search_tool_result`, so both
arrive through its union fallback carrying `text: None` on top of their real fields. Replaying that
null back to Anthropic is rejected with 400 «Extra inputs are not permitted», which is why the two
types have their own wire allowlist AND why `_replay_assistant_blocks` re-normalizes on the way out
(ADR-105 §A3) — rows persisted before the allowlist existed are healed instead of poisoning every
later continuation.
"""

from __future__ import annotations

from app.chat.anthropic_client import (
    _ANTHROPIC_ASSISTANT_INPUT_TYPES,
    _BLOCK_WIRE_FIELDS,
    _normalize_block,
    _replay_assistant_blocks,
)


def test_tool_use_strips_caller_keeps_wire_fields_and_raw_id() -> None:
    # ADR-021 root of problem 2: SDK adds `caller` to a tool_use block. ADR-008: raw id verbatim.
    raw = {
        "type": "tool_use",
        "id": "toolu_01ABCdef234567890XYZ",
        "name": "site_write_file",
        "input": {"path": "index.html", "content": "<h1>hi</h1>"},
        "caller": {"type": "direct"},
    }
    out = _normalize_block(raw)
    assert "caller" not in out
    assert out == {
        "type": "tool_use",
        "id": "toolu_01ABCdef234567890XYZ",  # raw provider id preserved verbatim (ADR-008)
        "name": "site_write_file",
        "input": {"path": "index.html", "content": "<h1>hi</h1>"},
    }


def test_tool_use_drops_any_future_non_wire_field_via_allowlist() -> None:
    # Allowlist (not point-removal of `caller`): any unknown SDK annotation is dropped too.
    raw = {
        "type": "tool_use",
        "id": "toolu_x",
        "name": "files_read",
        "input": {"path": "a"},
        "caller": {"type": "direct"},
        "some_future_sdk_field": {"nested": 1},
        "cache_control": {"type": "ephemeral"},
    }
    out = _normalize_block(raw)
    assert set(out.keys()) == {"type", "id", "name", "input"}


def test_text_block_keeps_only_type_and_text() -> None:
    raw = {"type": "text", "text": "hello world", "citations": None, "caller": {"type": "direct"}}
    assert _normalize_block(raw) == {"type": "text", "text": "hello world"}


def test_image_block_keeps_type_and_source() -> None:
    source = {"type": "base64", "media_type": "image/png", "data": "QUJD"}
    raw = {"type": "image", "source": source, "caller": {"type": "direct"}}
    assert _normalize_block(raw) == {"type": "image", "source": source}


def test_document_block_keeps_type_and_source() -> None:
    source = {"type": "base64", "media_type": "application/pdf", "data": "JVBE"}
    raw = {"type": "document", "source": source, "title": "x", "caller": {"type": "direct"}}
    assert _normalize_block(raw) == {"type": "document", "source": source}


def test_thinking_block_keeps_wire_fields() -> None:
    raw = {
        "type": "thinking",
        "thinking": "let me think",
        "signature": "sig123",
        "caller": {"type": "direct"},
    }
    assert _normalize_block(raw) == {
        "type": "thinking",
        "thinking": "let me think",
        "signature": "sig123",
    }


def test_redacted_thinking_block_keeps_data() -> None:
    raw = {"type": "redacted_thinking", "data": "encrypted-blob", "caller": {"type": "direct"}}
    assert _normalize_block(raw) == {"type": "redacted_thinking", "data": "encrypted-blob"}


def test_unknown_block_type_does_not_raise_and_only_drops_caller() -> None:
    # Forward-compat: an unknown future block type keeps all content, dropping only `caller`.
    raw = {"type": "future_block_xyz", "payload": {"k": "v"}, "caller": {"type": "direct"}}
    out = _normalize_block(raw)
    assert out == {"type": "future_block_xyz", "payload": {"k": "v"}}


def test_block_missing_type_does_not_raise() -> None:
    # Defensive: a block without a "type" key must not crash; treated as unknown → drop caller only.
    raw = {"caller": {"type": "direct"}, "stuff": 1}
    assert _normalize_block(raw) == {"stuff": 1}


def test_allowlist_keeps_only_present_fields_no_kerror() -> None:
    # A wire field absent in the block must not be invented (comprehension is membership-gated).
    raw = {"type": "tool_use", "id": "toolu_y", "name": "files_read"}  # no "input"
    out = _normalize_block(raw)
    assert out == {"type": "tool_use", "id": "toolu_y", "name": "files_read"}
    assert "input" not in out


def test_wire_allowlist_table_covers_documented_block_types() -> None:
    # Guard against accidental allowlist drift. NB: ADR-021 Decision §2 does NOT enumerate block
    # types (checked against the ADR text) — it mandates that normalization be an allowlist over
    # the block's wire schema "а не точечное удаление одного ключа `caller`" and fixes the wire
    # fields of `tool_use` only. This table IS the enumeration; the hosted-search pair below is
    # required by the Anthropic input schema for replay (ADR-105 §A3.6 / `_replay_assistant_blocks`)
    # and its absence is what returned 400 «Extra inputs are not permitted» in production.
    assert set(_BLOCK_WIRE_FIELDS) == {
        "text",
        "image",
        "document",
        "tool_use",
        "thinking",
        "redacted_thinking",
        "server_tool_use",
        "web_search_tool_result",
    }
    assert _BLOCK_WIRE_FIELDS["tool_use"] == ("type", "id", "name", "input")
    assert _BLOCK_WIRE_FIELDS["server_tool_use"] == ("type", "id", "name", "input")
    assert _BLOCK_WIRE_FIELDS["web_search_tool_result"] == ("type", "tool_use_id", "content")


# --- Hosted web search: SDK union fallback + replay healing ---------------------------------


def test_server_tool_use_union_fallback_cut_to_wire_fields() -> None:
    # The SDK validates every response block against `TextBlock | ToolUseBlock`; `server_tool_use`
    # matches neither, so it is built from the first variant and comes back with `text: None`.
    # `cache_control` is a NON-null extra on purpose: the no-allowlist branch's `v is not None`
    # safe would keep it, so this case is locked by the ALLOWLIST entry, not by the null safe.
    raw = {
        "text": None,
        "type": "server_tool_use",
        "id": "srvtoolu_01AbCdEfGhIjKlMnOpQr",
        "name": "web_search",
        "input": {"query": "anthropic block normalization"},
        "cache_control": {"type": "ephemeral"},
    }
    assert _normalize_block(raw) == {
        "type": "server_tool_use",
        "id": "srvtoolu_01AbCdEfGhIjKlMnOpQr",
        "name": "web_search",
        "input": {"query": "anthropic block normalization"},
    }


def test_web_search_tool_result_union_fallback_cut_to_wire_fields() -> None:
    # Same union fallback on the result half of the hosted-search pair. `cache_control` is again a
    # non-null extra, so the assertion fails if the allowlist entry is removed — not merely if the
    # null safe is removed.
    results = [
        {
            "type": "web_search_result",
            "url": "https://example.com/a",
            "title": "A",
            "encrypted_content": "RW5jcnlwdGVkQmxvYg==",
        }
    ]
    raw = {
        "text": None,
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_01AbCdEfGhIjKlMnOpQr",
        "content": results,
        "cache_control": {"type": "ephemeral"},
    }
    assert _normalize_block(raw) == {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_01AbCdEfGhIjKlMnOpQr",
        "content": results,
    }


def test_nested_web_search_results_are_not_touched() -> None:
    # Normalization is TOP-LEVEL only: the inner `web_search_result` items are raw wire objects and
    # must survive verbatim — `encrypted_content` is what Anthropic requires to replay the citation,
    # and a nested `page_age: None` is a legitimate wire value, not a fallback artifact.
    results = [
        {
            "type": "web_search_result",
            "url": "https://example.com/a",
            "title": "A",
            "encrypted_content": "RW5jcnlwdGVkQmxvYg==",
            "page_age": None,
        }
    ]
    raw = {
        "text": None,
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_nested",
        "content": results,
    }
    out = _normalize_block(raw)
    assert out["content"] is results  # same object: values are passed through, never rebuilt
    assert out["content"][0]["encrypted_content"] == "RW5jcnlwdGVkQmxvYg=="
    assert out["content"][0]["page_age"] is None


def test_replayable_type_without_allowlist_loses_only_the_null_text() -> None:
    # `mcp_tool_use` is defined by the Anthropic input schema (so replay keeps it) but has no wire
    # allowlist, so it goes through the fallback branch: the union artifact `text: None` is dropped
    # and every real field survives.
    assert "mcp_tool_use" in _ANTHROPIC_ASSISTANT_INPUT_TYPES
    assert "mcp_tool_use" not in _BLOCK_WIRE_FIELDS
    raw = {
        "text": None,
        "type": "mcp_tool_use",
        "id": "mcptoolu_01",
        "name": "search_docs",
        "server_name": "docs",
        "input": {"q": "adr-105"},
    }
    assert _normalize_block(raw) == {
        "type": "mcp_tool_use",
        "id": "mcptoolu_01",
        "name": "search_docs",
        "server_name": "docs",
        "input": {"q": "adr-105"},
    }


def test_unknown_future_type_keeps_every_non_null_field_including_empty_ones() -> None:
    # Forward-compat, sharpened: the fallback drops NULLS, not FALSY values. An empty list and a
    # zero are real wire content and must survive, otherwise a future hosted tool loses its payload.
    raw = {
        "type": "future_hosted_tool_result_v9",
        "tool_use_id": "srvtoolu_future",
        "content": [],
        "retry_count": 0,
        "caller": {"type": "direct"},
    }
    assert _normalize_block(raw) == {
        "type": "future_hosted_tool_result_v9",
        "tool_use_id": "srvtoolu_future",
        "content": [],
        "retry_count": 0,
    }


def test_replay_heals_a_dirty_persisted_hosted_search_step() -> None:
    # Regression on the ~1.8k rows already in `chat_steps.payload`: they were persisted BEFORE the
    # hosted-search types had an allowlist, so their blocks still carry `text: null`. Replaying such
    # a step verbatim is what Anthropic rejected with 400, so `_replay_assistant_blocks` must
    # re-normalize on the way out instead of trusting the stored row (ADR-105 §A3).
    results = [
        {
            "type": "web_search_result",
            "url": "https://example.com/1",
            "title": "T",
            "encrypted_content": "RW5j",
        }
    ]
    stored = [
        {"type": "text", "text": "Вот что нашлось:"},
        {
            "text": None,
            "type": "server_tool_use",
            "id": "srvtoolu_dirty",
            "name": "web_search",
            "input": {"query": "adr"},
        },
        {
            "text": None,
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_dirty",
            "content": results,
        },
    ]
    out = _replay_assistant_blocks(stored)
    assert out == [
        {"type": "text", "text": "Вот что нашлось:"},
        {
            "type": "server_tool_use",
            "id": "srvtoolu_dirty",
            "name": "web_search",
            "input": {"query": "adr"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_dirty",
            "content": results,
        },
    ]
    # The precise thing the provider rejects: an explicit null `text` key on a non-text block.
    # (`get` alone would not do: an ABSENT key also reads as None and is exactly what we want.)
    assert all("text" not in block or block["text"] is not None for block in out)
