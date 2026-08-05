"""Opaque keyset-pagination cursor for the generations feed (media-generation/02-api-contracts.md).

Encodes the ordering tuple ``(created_at, id)`` of the last returned row. The feed is ordered
``created_at DESC, id DESC`` — the id is a tie-break, because two jobs submitted in the same
millisecond would otherwise page non-deterministically.

Keyset and not ``offset``: the feed grows from the head, so with an offset a job created between
two page requests shifts the window and the user sees duplicates and gaps. Same shape as the
workspaces and chats cursors (ADR-036 §8) so the API has one pagination convention, not two.
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
class MediaJobCursor:
    created_at: datetime.datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(value: str) -> MediaJobCursor:
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            created_str, id_str = raw.split("|", 1)
            created = datetime.datetime.fromisoformat(created_str)
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.UTC)
            return MediaJobCursor(created_at=created, id=uuid.UUID(id_str))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("invalid cursor") from exc
