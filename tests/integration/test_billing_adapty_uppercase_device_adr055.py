"""Integration: ADR-055 rework — Adapty webhook credits a user whose device_id is stored UPPERCASE.

The REAL prod incident (broadnova/orvianix, tester 2c10eec7): Adapty sends our **deviceId** as
``customer_user_id`` (a UUID string); the matching ``auth_devices.device_id`` row is stored in
UPPERCASE (iOS ``identifierForVendor.uuidString``). The parser normalises the inbound id to a
``uuid.UUID`` (=> ``str`` lowercase), so the OLD exact-match ``WHERE device_id = :x`` never found
the uppercase row -> ``user_not_found`` -> the subscription silently lapsed. ADR-055 compares
``lower(device_id) = str(x)``, so the linked user is now credited.

This is the exact-shape end-to-end assertion the earlier ADR-055 suite MISSED: that suite seeded
the device_id lowercase (``str(D)``), so it stayed green with AND without the case-insensitive fix.
Here the device_id is seeded ``str(D).upper()`` — reverting branch (b) to exact-match turns
``test_uppercase_device_credits_linked_user`` RED (result ``ignored``/``user_not_found``, no grant).

Drives ``AdaptyWebhookService.handle()`` DIRECTLY against the REAL testcontainers Postgres.
Hermetic: no network, no real Adapty/LLM. In-process Alembic disables the service logger under
``caplog`` -> the autouse fixture re-enables it (test-harness artifact, see ADR-046 suite).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.billing_adapty.service import AdaptyWebhookService
from app.config import Settings
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "adapty_webhook_outcome"
_LOGGER = "app.billing_adapty.service"
_SECRET = "adapty-webhook-secret-value"  # noqa: S105 - test-only static secret
_WEEK_PRODUCT = "week_6.99_nottrial"
_WEEK_TOKENS = 700
_FALLBACK = 1000
_PRODUCT_TOKENS = json.dumps({_WEEK_PRODUCT: _WEEK_TOKENS})

# U is our internal userId; D is the tester's deviceId. Prod stores str(D).upper() in auth_devices
# (D matches the ADR incident: device_id = 'E8FF6CB8-77F1-4165-A91C-21E897E19C7A').
_U = uuid.UUID("894edaee-0902-4cf6-82d4-c4ca13787cb4")
_D = uuid.UUID("e8ff6cb8-77f1-4165-a91c-21e897e19c7a")


def _settings() -> Settings:
    return Settings(
        ADAPTY_WEBHOOK_SECRET=_SECRET,
        ADAPTY_PRODUCT_TOKENS=_PRODUCT_TOKENS,
        ADAPTY_SUBSCRIPTION_TOKENS_GRANT=_FALLBACK,
    )


@pytest.fixture(autouse=True)
def _enable_service_logger() -> None:
    logging.getLogger(_LOGGER).disabled = False


@pytest.fixture
async def adapty_service(db_session: AsyncSession) -> AsyncIterator[AdaptyWebhookService]:
    audit = AuditService(db_session)
    yield AdaptyWebhookService(db_session, WalletService(db_session, audit), audit, _settings())


def _event(*, profile_event_id: str, event_type: str, customer_user_id: str) -> bytes:
    body: dict[str, Any] = {
        "profile_event_id": profile_event_id,
        "event_type": event_type,
        "event_properties": {
            "transaction_id": 410003298316682,
            "vendor_product_id": _WEEK_PRODUCT,
        },
        "customer_user_id": customer_user_id,
    }
    return json.dumps(body).encode()


async def _seed_device_upper(
    maker: async_sessionmaker[AsyncSession], *, device_id: str, user_id: uuid.UUID
) -> None:
    async with maker() as s:
        await s.execute(
            text("INSERT INTO auth_devices (device_id, user_id) VALUES (:d, :u)"),
            {"d": device_id, "u": str(user_id)},
        )
        await s.commit()


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int | None:
    async with maker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        return None if bal is None else int(bal)


async def _subscription(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> tuple[str, str | None] | None:
    async with maker() as s:
        row = (
            await s.execute(
                text("SELECT status, plan FROM subscriptions WHERE user_id=:u"), {"u": str(uid)}
            )
        ).first()
    return None if row is None else (row.status, row.plan)


async def _ledger_keys(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> list[str]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT idempotency_key FROM ledger_transactions WHERE user_id=:u "
                    "ORDER BY created_at"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [r.idempotency_key for r in rows]


async def _event_user_ids(maker: async_sessionmaker[AsyncSession], event_id: str) -> list[str]:
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT user_id FROM adapty_webhook_events WHERE event_id=:e"),
                {"e": event_id},
            )
        ).all()
    return [str(r.user_id) for r in rows]


async def _audit_customer_ids(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> list[str]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM audit_logs WHERE user_id=:u "
                    "AND event_type='adapty_subscription' ORDER BY created_at"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [dict(r.payload)["customerId"] for r in rows]


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


@pytest.mark.asyncio
async def test_uppercase_device_credits_linked_user(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # THE incident shape: device_id stored UPPERCASE; customer_user_id = D (lowercased by the parser
    # via uuid.UUID). Every accrual lands on the RESOLVED U; the log carries customerUserId=D (the
    # original, still lowercase-canonical) and resolvedUserId=U, resolvedVia="device_id".
    stored = str(_D).upper()
    assert stored != str(_D)  # guard: really uppercase
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device_upper(db_sessionmaker, device_id=stored, user_id=_U)
    caplog.set_level(logging.DEBUG)

    # Adapty sends the id verbatim (uppercase on the wire); the parser normalises it to a UUID.
    raw = _event(
        profile_event_id="evt-upper",
        event_type="subscription_renewed",
        customer_user_id=stored,
    )
    outcome = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert outcome.result == "applied", "uppercase-stored device must resolve+credit (ADR-055 fix)"

    # ALL records target the resolved U.
    assert await _event_user_ids(db_sessionmaker, "evt-upper") == [str(_U)]
    assert await _subscription(db_sessionmaker, _U) == ("active", _WEEK_PRODUCT)
    assert await _balance(db_sessionmaker, _U) == _WEEK_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == ["adapty-txn:410003298316682"]
    # Audit preserves the ORIGINAL Adapty identifier (canonical lowercase UUID) for tracing.
    assert await _audit_customer_ids(db_sessionmaker, _U) == [str(_D)]

    # D never appears as a user_id anywhere.
    assert await _subscription(db_sessionmaker, _D) is None
    assert await _balance(db_sessionmaker, _D) is None
    assert await _ledger_keys(db_sessionmaker, _D) == []

    fields = _rendered(_outcomes(caplog)[0])
    assert fields["resolvedVia"] == "device_id"
    assert fields["resolvedUserId"] == str(_U)  # the real recipient
    assert fields["customerUserId"] == str(_D)  # canonical (lowercase) original id


@pytest.mark.asyncio
async def test_lowercase_device_still_credits_backward_compat(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Backward compat (avelyra/veltrio store lowercase): a lowercase-stored device still credits U.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device_upper(db_sessionmaker, device_id=str(_D), user_id=_U)

    raw = _event(
        profile_event_id="evt-lower", event_type="subscription_started", customer_user_id=str(_D)
    )
    outcome = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _WEEK_TOKENS
    assert await _event_user_ids(db_sessionmaker, "evt-lower") == [str(_U)]
