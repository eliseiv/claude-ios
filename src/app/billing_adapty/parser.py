"""Pure defensive parsing of an Adapty webhook payload (ADR-029 §3, billing-adapty/03).

Adapty does not version its payload strictly: the same logical field can arrive under different
keys across SDK / payload versions. These functions read a best-effort value from the parsed JSON
object without ever raising on missing / wrongly-typed fields (nested access is ``isinstance``
guarded). The body is parsed manually (no Pydantic) so a malformed verification ping yields an
``ignored`` outcome rather than a 422 (which Adapty would retry forever).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

# Recognised, normalised (lower-case) Adapty subscription event types (ADR-029 §4).
EVENT_STARTED = "subscription_started"
EVENT_RENEWED = "subscription_renewed"
EVENT_CANCELLED = "subscription_cancelled"
EVENT_EXPIRED = "subscription_expired"

GRANTING_EVENTS = frozenset({EVENT_STARTED, EVENT_RENEWED})
EXPIRING_EVENTS = frozenset({EVENT_CANCELLED, EVENT_EXPIRED})
KNOWN_EVENTS = GRANTING_EVENTS | EXPIRING_EVENTS


@dataclass(frozen=True)
class ParsedEvent:
    """A defensively parsed Adapty event. ``customer_user_id`` is already a validated UUID."""

    event_id: str
    event_type: str
    customer_user_id: uuid.UUID
    vendor_product_id: str | None
    expires_at: datetime.datetime | None


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, else an empty dict (safe nested access)."""
    return value if isinstance(value, dict) else {}


def _first_str(*candidates: Any) -> str | None:
    """First candidate that is a non-empty string, else None."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def parse_event_id(body: dict[str, Any]) -> str | None:
    return _first_str(body.get("event_id"), body.get("id"))


def parse_event_type(body: dict[str, Any]) -> str:
    raw = body.get("event_type")
    return raw.lower() if isinstance(raw, str) else ""


def parse_customer_user_id(body: dict[str, Any]) -> uuid.UUID | None:
    """customer_user_id (our userId UUID) from the documented source order (ADR-029 §3).

    Sources: ``customer_user_id`` -> ``profile.customer_user_id`` -> ``user_id``. A value that is
    absent or not a valid UUID is treated as "user not found" by the caller.
    """
    profile = _as_dict(body.get("profile"))
    raw = _first_str(
        body.get("customer_user_id"),
        profile.get("customer_user_id"),
        body.get("user_id"),
    )
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def parse_vendor_product_id(body: dict[str, Any]) -> str | None:
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        props.get("vendor_product_id"),
        props.get("product_id"),
        body.get("vendor_product_id"),
        body.get("product_id"),
    )


def parse_expires_at(body: dict[str, Any]) -> datetime.datetime | None:
    """ISO8601 ``expires_at`` -> tz-aware datetime; unparseable -> None (event still applied)."""
    props = _as_dict(body.get("event_properties"))
    profile = _as_dict(body.get("profile"))
    raw = _first_str(props.get("expires_at"), profile.get("expires_at"))
    if raw is None:
        return None
    # Accept a trailing 'Z' (Python <3.11 fromisoformat rejected it; 3.12 accepts, but normalise
    # defensively for any 'Z' variant). Unparseable -> None, not an error.
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed
