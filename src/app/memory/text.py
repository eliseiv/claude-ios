"""Extract searchable plain text from chat step payloads."""

from __future__ import annotations

from typing import Any

from app.chats.provider_blocks import to_domain_blocks
from app.chats.repository import strip_context_block


def extract_step_text(payload: dict[str, Any], *, role: str) -> str | None:
    """Return plain text for indexing, or None when there is nothing to index."""
    if role == "tool":
        return None
    parts: list[str] = []
    for block in to_domain_blocks(payload.get("content")):
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                parts.append(text)
    combined = "\n".join(parts).strip()
    if not combined:
        return None
    if role == "user":
        combined = strip_context_block(combined).strip()
    return combined or None


def chunk_text(text: str, *, max_chars: int, overlap: int) -> list[str]:
    """Split long text into overlapping chunks for embedding."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks
