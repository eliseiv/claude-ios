"""Opaque keyset-pagination cursors for chats list + chat history (chats/02-api-contracts.md).

List cursor encodes ``(is_pinned, updated_at, id)``. History cursor encodes ``(seq, id)`` so
scroll-up pages resume on the ADR-021 step order (not ``created_at``).
"""

from __future__ import annotations

import base64
import binascii
import datetime
import uuid
from dataclasses import dataclass


class InvalidCursorError(ValueError):
    """Raised when an opaque cursor cannot be decoded → mapped to 422 at the API layer."""


@dataclass(frozen=True)
class ChatCursor:
    is_pinned: bool
    updated_at: datetime.datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{int(self.is_pinned)}|{self.updated_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(value: str) -> ChatCursor:
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            pinned_str, updated_str, id_str = raw.split("|", 2)
            updated = datetime.datetime.fromisoformat(updated_str)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=datetime.UTC)
            return ChatCursor(
                is_pinned=bool(int(pinned_str)),
                updated_at=updated,
                id=uuid.UUID(id_str),
            )
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("invalid cursor") from exc


@dataclass(frozen=True)
class ChatHistoryCursor:
    """Keyset cursor for ``GET /v1/chats/{id}`` pages that walk toward older steps."""

    seq: int
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.seq}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(value: str) -> ChatHistoryCursor:
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            seq_str, id_str = raw.split("|", 1)
            return ChatHistoryCursor(seq=int(seq_str), id=uuid.UUID(id_str))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("invalid cursor") from exc
