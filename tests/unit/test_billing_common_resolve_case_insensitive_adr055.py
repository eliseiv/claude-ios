"""Unit: ADR-055 rework — ``resolve_user`` matches ``auth_devices.device_id`` CASE-INSENSITIVELY.

The prod incident this pins: iOS stores ``identifierForVendor.uuidString`` in UPPERCASE, so a large
share of ``auth_devices.device_id`` rows are uppercase (63/123 on broadnova, 23/75 on orvianix as of
2026-07). ``str(uuid.UUID)`` is ALWAYS lowercase (RFC 4122 normalisation), so the old exact-match
``WHERE device_id = :x`` silently dropped every uppercase-stored device — including the tester the
fix targets. Branch (b) now compares ``lower(device_id) = str(x)``; these tests seed the device_id
in UPPERCASE / MIXED case (the shapes the earlier suite never covered — it only seeded lowercase,
so it stayed green with AND without the fix) and assert the linked user still resolves.

REGRESSION GUARD: reverting branch (b) to ``WHERE device_id = :x`` MUST turn
``test_resolve_uppercase_stored_device_matches_lowercase_uuid`` (and the mixed-case case) RED — the
uppercase row would no longer be found and the resolve returns ``None``.

Hermetic: REAL testcontainers Postgres (via ``seed_user`` + a direct ``auth_devices`` insert), no
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

# U is our internal userId (a ``users`` row); D is a deviceId (``auth_devices.device_id`` -> U) that
# is NOT itself a ``users`` row. str(D) is lowercase; the prod rows store str(D).upper().
_U = uuid.UUID("1f6c4d2e-9a7b-4c3d-8e1f-2a3b4c5d6e7f")
_D = uuid.UUID("e8ff6cb8-77f1-4165-a91c-21e897e19c7a")  # the ADR incident device shape


async def _seed_device_raw(session: AsyncSession, *, device_id: str, user_id: uuid.UUID) -> None:
    """Insert an auth_devices row with the device_id stored VERBATIM (casing preserved)."""
    await session.execute(
        text("INSERT INTO auth_devices (device_id, user_id) VALUES (:d, :u)"),
        {"d": device_id, "u": str(user_id)},
    )
    await session.commit()


@pytest.mark.asyncio
async def test_resolve_uppercase_stored_device_matches_lowercase_uuid(
    db_session: AsyncSession,
) -> None:
    # THE incident: device_id stored UPPERCASE (str(D).upper()), resolve called with D whose
    # str(D) is lowercase. Old exact-match dropped this; lower(device_id)=str(x) matches it.
    stored = str(_D).upper()
    assert stored != str(_D)  # guard: the row really is uppercase, not accidentally lowercase
    await seed_user(db_session, user_id=_U)
    await _seed_device_raw(db_session, device_id=stored, user_id=_U)

    resolved = await resolve_user(db_session, _D)

    assert resolved is not None, "uppercase-stored device_id must resolve (ADR-055 fix)"
    linked_user_id, via = resolved
    assert via == RESOLVED_VIA_DEVICE_ID
    assert isinstance(linked_user_id, uuid.UUID)
    assert linked_user_id == _U
    assert linked_user_id != _D  # the resolved id is the USER, never the deviceId


@pytest.mark.asyncio
async def test_resolve_lowercase_stored_device_still_matches(db_session: AsyncSession) -> None:
    # Backward compat (avelyra/veltrio store lowercase): a lowercase-stored device still resolves.
    await seed_user(db_session, user_id=_U)
    await _seed_device_raw(db_session, device_id=str(_D), user_id=_U)

    resolved = await resolve_user(db_session, _D)

    assert resolved == (_U, RESOLVED_VIA_DEVICE_ID)


@pytest.mark.asyncio
async def test_resolve_mixed_case_stored_device_matches(db_session: AsyncSession) -> None:
    # A device_id stored in MIXED case also resolves (lower() normalises both sides).
    mixed = "E8ff6CB8-77F1-4165-A91c-21E897e19C7A"
    assert mixed.lower() == str(_D) and mixed not in (str(_D), str(_D).upper())
    await seed_user(db_session, user_id=_U)
    await _seed_device_raw(db_session, device_id=mixed, user_id=_U)

    resolved = await resolve_user(db_session, _D)

    assert resolved is not None
    linked_user_id, via = resolved
    assert (linked_user_id, via) == (_U, RESOLVED_VIA_DEVICE_ID)


@pytest.mark.asyncio
async def test_resolve_users_wins_over_uppercase_device_first_match(
    db_session: AsyncSession,
) -> None:
    # first-match wins even when the device is uppercase: X is BOTH a users row AND an uppercase
    # auth_devices.device_id linked to a DIFFERENT user -> branch (a) returns before (b) is reached.
    other = uuid.uuid4()
    await seed_user(db_session, user_id=_U)
    await seed_user(db_session, user_id=other)
    await _seed_device_raw(db_session, device_id=str(_U).upper(), user_id=other)

    resolved = await resolve_user(db_session, _U)

    assert resolved == (_U, RESOLVED_VIA_USER_ID)
    assert resolved != (other, RESOLVED_VIA_DEVICE_ID)


@pytest.mark.asyncio
async def test_resolve_unknown_uppercase_absent_returns_none(db_session: AsyncSession) -> None:
    # A well-formed UUID present in NEITHER table (even upper/lower) -> None (no provisioning).
    await seed_user(db_session, user_id=_U)
    await _seed_device_raw(db_session, device_id=str(_D).upper(), user_id=_U)
    stranger = uuid.uuid4()

    assert await resolve_user(db_session, stranger) is None
