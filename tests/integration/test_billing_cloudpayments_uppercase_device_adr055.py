"""Integration: ADR-055 rework — CloudPayments webhook credits a user with an UPPERCASE device_id.

CloudPayments (broadapps RU flow) sends our **deviceId** as ``AccountId``; the linked
``auth_devices.device_id`` may be stored UPPERCASE (iOS ``identifierForVendor.uuidString``). The
CP webhook inherits the case-insensitive fix through the SHARED ``resolve_user`` (ADR-055 §A): the
old exact-match ``WHERE device_id = :x`` dropped uppercase-stored devices -> ``user_not_found`` ->
no credit despite a real payment; ``lower(device_id) = str(x)`` now credits the linked user.

The earlier CP suite only proved INBOUND normalisation (``AccountId`` sent UPPER, device stored
LOWER) — it never seeded an UPPERCASE device row, so it stayed green with AND without the fix. Here
the STORED device_id is ``str(D).upper()``: reverting branch (b) to exact-match turns
``test_uppercase_stored_device_credits_linked_user`` RED (ignored/user_not_found, verify skipped).

Drives ``CloudPaymentsWebhookService.handle()`` DIRECTLY with a FAKE verify client (no network)
against the REAL testcontainers Postgres. In-process Alembic disables the service logger under
``caplog`` -> re-enabled by the autouse fixture.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.config import Settings
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "cloudpayments_webhook_outcome"
_LOGGER = "app.billing_cloudpayments.service"
_SUB_CODE = "week_6.99_nottrial"
_SUB_TOKENS = 12000

# U is our internal userId; D is the tester's deviceId, stored UPPERCASE in auth_devices.
_U = uuid.UUID("b0f407bd-4a19-449e-beab-84ce341d6915")
_D = uuid.UUID("e8ff6cb8-77f1-4165-a91c-21e897e19c7a")


class FakeVerifyClient:
    def __init__(self, *, payments: list[dict[str, Any]] | None = None) -> None:
        self._payments = payments if payments is not None else []
        self.calls: list[uuid.UUID] = []

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        self.calls.append(device_id)
        return [dict(p) for p in self._payments]


def _payment(payment_id: str, *, code: str, payment_type: str) -> dict[str, Any]:
    return {
        "payment_id": payment_id,
        "status": "succeeded",
        "paid_at": (
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=5)
        ).isoformat(),
        "product": {"code": code, "payment_type": payment_type},
    }


def _settings() -> Settings:
    return Settings(
        CLOUDPAYMENTS_API_TOKEN="verify-token-secret",  # noqa: S106 - test-only; config gate must pass
        CLOUDPAYMENTS_PRODUCT_TOKENS=json.dumps({_SUB_CODE: _SUB_TOKENS}),
        CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT=1000,
        TOKEN_PRODUCTS=json.dumps({}),
    )


def _service(session: AsyncSession, verify: FakeVerifyClient) -> CloudPaymentsWebhookService:
    audit = AuditService(session)
    return CloudPaymentsWebhookService(
        session, WalletService(session, audit), audit, _settings(), verify
    )


def _body(*, account_id: str) -> bytes:
    body = {
        "Status": "Completed",
        "OperationType": "Payment",
        "Amount": 3990,
        "Currency": "RUB",
        "AccountId": account_id,
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


async def _event_user_ids(maker: async_sessionmaker[AsyncSession], txn: str) -> list[str]:
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT user_id FROM cloudpayments_webhook_events WHERE transaction_id=:t"),
                {"t": txn},
            )
        ).all()
    return [str(r.user_id) for r in rows]


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


@pytest.fixture(autouse=True)
def _enable_service_logger() -> None:
    logging.getLogger(_LOGGER).disabled = False


@pytest.mark.asyncio
async def test_uppercase_stored_device_credits_linked_user(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # THE incident shape: device_id stored UPPERCASE; the callback carries D (parser normalises it
    # to a lowercase-canonical UUID). Old exact-match dropped it; lower(device_id)=str(x) credits U.
    stored = str(_D).upper()
    assert stored != str(_D)  # guard: really uppercase
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    await _seed_device_upper(db_sessionmaker, device_id=stored, user_id=_U)
    caplog.set_level(logging.DEBUG)

    verify = FakeVerifyClient(
        payments=[_payment("sub-1", code=_SUB_CODE, payment_type="subscription")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_D)))
    assert outcome.result == "applied", "uppercase-stored device must resolve+credit (ADR-055 fix)"
    # verify is queried on the canonical (lowercase) deviceId, never the resolved userId.
    assert verify.calls == [_D]

    assert await _subscription(db_sessionmaker, _U) == ("active", _SUB_CODE)
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:sub-1"]
    assert await _event_user_ids(db_sessionmaker, "sub-1") == [str(_U)]

    # D never appears as a user_id anywhere.
    assert await _subscription(db_sessionmaker, _D) is None
    assert await _balance(db_sessionmaker, _D) is None
    assert await _ledger_keys(db_sessionmaker, _D) == []

    fields = _rendered(_outcomes(caplog)[0])
    assert fields["resolvedVia"] == "device_id"
    assert fields["userId"] == str(_U)


@pytest.mark.asyncio
async def test_uppercase_wire_and_uppercase_store_both_normalise(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Belt-and-suspenders: BOTH the callback AccountId AND the stored device_id are UPPERCASE ->
    # inbound-normalisation (parse to UUID) AND lower(device_id) together still resolve to U.
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    await _seed_device_upper(db_sessionmaker, device_id=str(_D).upper(), user_id=_U)

    verify = FakeVerifyClient(
        payments=[_payment("sub-2", code=_SUB_CODE, payment_type="subscription")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_D).upper()))
    assert outcome.result == "applied"
    assert verify.calls == [_D]  # normalised to the canonical lowercase deviceId
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
