"""Opaque keyset cursor for GET /v1/scheduled-chats (newest-first by created_at)."""

from __future__ import annotations

import base64
import binascii
import datetime
import uuid
from dataclasses import dataclass


class InvalidCursorError(ValueError):
    """Opaque cursor cannot be decoded → 422 at the API layer."""


@dataclass(frozen=True)
class ScheduledChatCursor:
    created_at: datetime.datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(value: str) -> ScheduledChatCursor:
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            created_str, id_str = raw.split("|", 1)
            created = datetime.datetime.fromisoformat(created_str)
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.UTC)
            return ScheduledChatCursor(created_at=created, id=uuid.UUID(id_str))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("invalid cursor") from exc
