"""Unit: the opaque feed cursor (ADR-063 §3).

The cursor is client-visible and therefore client-mutable. Two properties matter: it round-trips
exactly (a lost microsecond or a normalized timezone would silently re-serve or skip a row at the
page boundary), and anything unparseable raises rather than degrading into "start from the top",
which would loop the feed forever.
"""

from __future__ import annotations

import base64
import datetime
import uuid

import pytest

from app.media_generation.cursor import InvalidCursorError, MediaJobCursor


def test_cursor_round_trips_exactly() -> None:
    original = MediaJobCursor(
        created_at=datetime.datetime(2026, 8, 5, 11, 20, 31, 482913, tzinfo=datetime.UTC),
        id=uuid.UUID("e1f0c8a2-3b4d-4e5f-8a9b-0c1d2e3f4a5b"),
    )

    assert MediaJobCursor.decode(original.encode()) == original


def test_a_non_utc_offset_survives_as_the_same_instant() -> None:
    tz = datetime.timezone(datetime.timedelta(hours=3))
    original = MediaJobCursor(
        created_at=datetime.datetime(2026, 8, 5, 14, 20, 31, tzinfo=tz), id=uuid.uuid4()
    )

    decoded = MediaJobCursor.decode(original.encode())

    assert decoded.created_at == original.created_at


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Postgres hands back aware datetimes, but a hand-built cursor must not compare as naive."""
    raw = f"2026-08-05T11:20:31.482913|{uuid.uuid4()}"
    encoded = base64.urlsafe_b64encode(raw.encode()).decode()

    assert MediaJobCursor.decode(encoded).created_at.tzinfo is datetime.UTC


def test_the_cursor_is_url_safe() -> None:
    encoded = MediaJobCursor(
        created_at=datetime.datetime.now(tz=datetime.UTC), id=uuid.uuid4()
    ).encode()

    assert "+" not in encoded
    assert "/" not in encoded


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-base64!!",
        base64.urlsafe_b64encode(b"no separator here").decode(),
        base64.urlsafe_b64encode(b"2026-08-05T11:20:31|not-a-uuid").decode(),
        base64.urlsafe_b64encode(b"not-a-date|e1f0c8a2-3b4d-4e5f-8a9b-0c1d2e3f4a5b").decode(),
        base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode(),
    ],
)
def test_garbage_raises_instead_of_restarting_the_feed(value: str) -> None:
    with pytest.raises(InvalidCursorError):
        MediaJobCursor.decode(value)
