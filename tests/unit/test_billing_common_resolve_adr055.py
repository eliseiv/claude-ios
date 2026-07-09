"""Unit: ADR-055 — the shared two-step webhook user resolver ``billing_common.resolve_user``.

``resolve_user(session, X)`` is the single source of truth both payment webhooks (Adapty,
CloudPayments) now call. It maps the aggregator identifier ``X`` (a deviceId or our userId) to our
internal ``userId`` in two deterministic steps, first-match wins:

- (a) ``X`` in ``users`` -> ``(X, "user_id")`` — X already IS our userId (backward compat);
- (b) else ``X`` in ``auth_devices.device_id`` -> ``(linked user_id, "device_id")`` — the incident
  fix (Adapty/broadapps send a deviceId, not our userId);
- (c) else -> ``None`` (never provision, ADR-007).

``resolve_user`` is a pure DB operation (two SELECTs), so it is exercised against the REAL
testcontainers Postgres seeded via ``seed_user`` + a direct ``auth_devices`` insert. Hermetic: no
network, no LLM, no external services.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing_common.resolve import (
    RESOLVED_VIA_DEVICE_ID,
    RESOLVED_VIA_USER_ID,
    resolve_user,
)
from tests.conftest import seed_user

# U is our internal userId (a ``users`` row); D is a deviceId (an ``auth_devices.device_id`` -> U)
# that is NOT itself a ``users`` row.
_U = uuid.UUID("b0f407bd-4a19-449e-beab-84ce341d6915")
_D = uuid.UUID("55cbe083-fcbd-4460-af62-06f9a7bea97c")


async def _seed_device(session: AsyncSession, *, device_id: str, user_id: uuid.UUID) -> None:
    await session.execute(
        text("INSERT INTO auth_devices (device_id, user_id) VALUES (:d, :u)"),
        {"d": device_id, "u": str(user_id)},
    )
    await session.commit()


def test_resolved_via_constant_values() -> None:
    # The log/audit ``resolvedVia`` allowlist values are contractually fixed (ADR-055 §A / §C).
    assert RESOLVED_VIA_USER_ID == "user_id"
    assert RESOLVED_VIA_DEVICE_ID == "device_id"


@pytest.mark.asyncio
async def test_resolve_user_in_users_returns_user_id(db_session: AsyncSession) -> None:
    # (a) X present in users -> X already IS our userId, resolved_via="user_id".
    await seed_user(db_session, user_id=_U)

    resolved = await resolve_user(db_session, _U)

    assert resolved == (_U, RESOLVED_VIA_USER_ID)


@pytest.mark.asyncio
async def test_resolve_device_only_returns_linked_user_via_device_id(
    db_session: AsyncSession,
) -> None:
    # (b) X only in auth_devices.device_id (the prod incident: X is a deviceId) -> linked user_id,
    # resolved_via="device_id". X itself is NOT a users row.
    await seed_user(db_session, user_id=_U)
    await _seed_device(db_session, device_id=str(_D), user_id=_U)

    resolved = await resolve_user(db_session, _D)

    assert resolved is not None
    linked_user_id, via = resolved
    assert via == RESOLVED_VIA_DEVICE_ID
    # Normalised to a real uuid.UUID (resolve_user does uuid.UUID(str(...))), equal to the linked U.
    assert isinstance(linked_user_id, uuid.UUID)
    assert linked_user_id == _U
    assert linked_user_id != _D  # the resolved id is the USER, never the deviceId


@pytest.mark.asyncio
async def test_resolve_unknown_returns_none(db_session: AsyncSession) -> None:
    # (c) X in neither table -> None (no provisioning, ADR-007). An unrelated user exists.
    await seed_user(db_session, user_id=_U)
    stranger = uuid.uuid4()

    assert await resolve_user(db_session, stranger) is None


@pytest.mark.asyncio
async def test_resolve_first_match_users_wins_over_device(db_session: AsyncSession) -> None:
    # first-match wins: if X is BOTH a users row AND an auth_devices.device_id (linked to a
    # DIFFERENT user), path (a) wins -> (X, "user_id"); the device link is NOT followed.
    other = uuid.uuid4()
    await seed_user(db_session, user_id=_U)
    await seed_user(db_session, user_id=other)
    await _seed_device(db_session, device_id=str(_U), user_id=other)

    resolved = await resolve_user(db_session, _U)

    assert resolved == (_U, RESOLVED_VIA_USER_ID)
    assert resolved != (other, RESOLVED_VIA_DEVICE_ID)
