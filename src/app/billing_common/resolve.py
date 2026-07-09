"""Two-step webhook user resolution: deviceId/userId -> our internal userId (ADR-055).

Single source of truth shared by every payment webhook (Adapty, CloudPayments, and any future
contour). Payment aggregators send our ``customer_user_id`` / ``AccountId`` as a **deviceId** (the
id the client passed to ``Adapty.identify`` / broadapps), not our JWT ``userId``. Both webhooks
previously only checked ``users.id`` and dropped a real payment as ``user_not_found``; the
deviceId -> userId link lives in our own ``auth_devices`` table (ADR-018). The body of this function
was ported verbatim from ``CloudPaymentsWebhookService._resolve_user`` (ADR-053) so behaviour is
byte-for-byte identical; both webhooks now call it (ADR-055 §A).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RESOLVED_VIA_USER_ID = "user_id"
RESOLVED_VIA_DEVICE_ID = "device_id"


async def resolve_user(session: AsyncSession, x: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    """Resolve the webhook identifier ``X`` to our internal ``userId`` (ADR-055, two-step).

    Aggregators send a deviceId (not our userId) as the customer identifier. First-match wins,
    deterministic (``users`` before ``auth_devices`` for compat):

    - (a) ``X`` in ``users`` -> ``X`` already IS our userId; ``resolved_via = "user_id"``.
    - (b) else ``lower(X)`` matches ``lower(auth_devices.device_id)`` -> take the linked ``user_id``
      (deviceId -> userId); ``resolved_via = "device_id"``. This is the incident fix.
    - (c) else -> ``None`` (=> ``user_not_found``; we never provision users/devices, ADR-007).

    The deviceId->userId mapping is taken ONLY from our ``auth_devices`` (never from the webhook
    body). ``auth_devices.device_id`` is a ``TEXT`` PK that stores the deviceId verbatim in the
    client's casing; iOS sends ``identifierForVendor.uuidString`` in UPPERCASE, so a large share of
    rows (e.g. 63/123 on broadnova, 23/75 on orvianix as of 2026-07) are uppercase. ``str(x)`` is
    always lowercase (``str(uuid.UUID)`` normalizes per RFC 4122, which makes the UUID textually
    case-insensitive), so branch (b) MUST compare case-insensitively via ``lower(device_id)`` — an
    exact match would drop the very users this fix targets (ADR-055 rework). Branch (a) is
    untouched: ``users.id`` is a real ``uuid`` column where casing is irrelevant.
    """
    if await session.scalar(
        text("SELECT 1 FROM users WHERE id = :x"),
        {"x": str(x)},
    ):
        return x, RESOLVED_VIA_USER_ID

    # str(x) is already lowercase; compare against lower(device_id). ORDER BY user_id LIMIT 1 makes
    # the pick deterministic under a hypothetical casing collision (two auth_devices rows with the
    # same lower(device_id) but different raw casing). Prod is collision-free today
    # (lower(device_id) unique per user across all instances, verified ADR-055);
    # ix_auth_devices_lower_device_id is deliberately non-unique, so the ORDER BY guards against a
    # future invariant-violating row from crediting the wrong user silently — this is money, a
    # silent arbitrary pick is unacceptable.
    device_user_id = await session.scalar(
        text(
            "SELECT user_id FROM auth_devices WHERE lower(device_id) = :x ORDER BY user_id LIMIT 1"
        ),
        {"x": str(x)},
    )
    if device_user_id is not None:
        return uuid.UUID(str(device_user_id)), RESOLVED_VIA_DEVICE_ID

    return None
