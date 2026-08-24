"""Unit tests for memory text extraction and intent detection."""

from __future__ import annotations

from app.memory.retriever import should_auto_retrieve
from app.memory.text import chunk_text, extract_step_text


def test_extract_user_text_strips_context_block() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": "[Conversation settings for this message: codeLanguage=swift]\n\nHello SwiftUI",
            }
        ]
    }
    assert extract_step_text(payload, role="user") == "Hello SwiftUI"


def test_chunk_text_splits_long_body() -> None:
    text = "a" * 2000
    parts = chunk_text(text, max_chars=1000, overlap=100)
    assert len(parts) == 3
    assert parts[0] == "a" * 1000


def test_should_auto_retrieve_russian_phrase() -> None:
    assert should_auto_retrieve("Что мы обсуждали про onboarding?")
    assert not should_auto_retrieve("Напиши функцию sort")
