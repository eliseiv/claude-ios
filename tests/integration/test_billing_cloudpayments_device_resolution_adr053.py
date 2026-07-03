"""Integration: ADR-053 — two-step userId resolution in the CloudPayments webhook.

The RU flow (broadapps) sends a **deviceId** as ``AccountId``/``Data.user_id``, NOT our internal
``userId``. ADR-053 resolves the callback identifier ``X`` in two steps against the REAL Postgres
container: (a) ``X`` in ``users`` -> it IS our userId (``resolvedVia="user_id"``); (b) else ``X`` in
``auth_devices.device_id`` -> take the linked ``user_id`` (deviceId -> userId,
``resolvedVia="device_id"``); (c) else -> ``user_not_found``. All accrual (subscription / wallet /
ledger / dedup / audit) keys on the RESOLVED userId, never on the deviceId.

Drives ``CloudPaymentsWebhookService.handle()`` DIRECTLY (the outcome log with ``resolvedVia`` lives
in the service; the router collapses everything to ``{"code":0}``). Hermetic: DB is the shared
testcontainers Postgres (seeded via ``seed_user`` + a direct ``auth_devices`` insert); ``Settings``
is built inline; no network to broadapps/YooKassa; no LLM. Mirrors the working webhook / reasons
suites (in-process migration disables the service logger under ``caplog`` -> re-enabled in the
fixture).
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
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.config import Settings
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "cloudpayments_webhook_outcome"
_SUB_PRODUCT = "yearly_49.99_nottrial"
_SUB_TOKENS = 12000
_FALLBACK = 1000
_TOKEN_PRODUCT = "100_tokens_9.99"
_TOKEN_CREDITS = 100
_TXN = "31d884c8-000f-5001-8000-1fb75b44e1d9"

# Distinct UUID spaces: U is our internal userId (in ``users``); D is a deviceId (in
# ``auth_devices.device_id``, linked -> U) that is NOT itself a ``users`` row.
_U = uuid.UUID("b0f407bd-4a19-449e-beab-84ce341d6915")
_D = uuid.UUID("55cbe083-fcbd-4460-af62-06f9a7bea97c")


def _settings() -> Settings:
    return Settings(
        CLOUDPAYMENTS_WEBHOOK_TOKEN="x",  # noqa: S106 - test-only static secret
        CLOUDPAYMENTS_PRODUCT_TOKENS=json.dumps({_SUB_PRODUCT: _SUB_TOKENS}),
        CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT=_FALLBACK,
        TOKEN_PRODUCTS=json.dumps({_TOKEN_PRODUCT: _TOKEN_CREDITS}),
    )


@pytest.fixture
async def cp_service(db_session: AsyncSession) -> AsyncIterator[CloudPaymentsWebhookService]:
    """Service bound to the container DB session (real Wallet/Audit, inline Settings).

    Re-enable the service logger: the in-process Alembic migration
    (``disable_existing_loggers=True``) disables ``app.billing_cloudpayments.service``, which would
    otherwise swallow records under ``caplog`` (test-harness artifact only).
    """
    logging.getLogger("app.billing_cloudpayments.service").disabled = False
    audit = AuditService(db_session)
    yield CloudPaymentsWebhookService(
        db_session, WalletService(db_session, audit), audit, _settings()
    )


# --------------------------- payload builder (CloudPayments wire form) ---------------------------


def _body(
    *,
    data: dict[str, Any] | str,
    account_id: str,
    transaction_id: str | None = _TXN,
    status: str = "Completed",
    operation_type: str = "Payment",
    amount: int = 3990,
) -> bytes:
    body: dict[str, Any] = {
        "Status": status,
        "OperationType": operation_type,
        "Amount": amount,
        "Currency": "RUB",
        "AccountId": account_id,
        "Data": json.dumps(data) if isinstance(data, dict) else data,
    }
    if transaction_id is not None:
        body["TransactionId"] = transaction_id
    return json.dumps(body).encode()


def _sub_data(account_id: str, product_id: str = _SUB_PRODUCT, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "user_id": account_id,
        "product_id": product_id,
        "billing_interval_unit": "year",
        "billing_interval_count": "1",
    }
    d.update(extra)
    return d


# --------------------------- DB helpers ---------------------------


async def _seed_device(
    maker: async_sessionmaker[AsyncSession], *, device_id: str, user_id: uuid.UUID
) -> None:
    """Insert an ``auth_devices`` row (deviceId -> userId), as ``/v1/auth/register`` would."""
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
                text("SELECT status, plan FROM subscriptions WHERE user_id=:u"),
                {"u": str(uid)},
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


# ============================ §1 device-resolve (the core fix) ============================


@pytest.mark.asyncio
async def test_device_resolve_subscription_credits_the_linked_user(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # U is our user; auth_devices[D] -> U; the callback carries D (a deviceId, NOT in users).
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)
    caplog.set_level(logging.DEBUG)

    outcome = await cp_service.handle(_body(data=_sub_data(str(_D)), account_id=str(_D)))
    await cp_service._session.commit()
    assert outcome.result == "applied"

    # Accrual is on U (the resolved userId), NOT on D (the deviceId).
    sub = await _subscription(db_sessionmaker, _U)
    assert sub == ("active", _SUB_PRODUCT)
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == [f"cp-txn:{_TXN}"]
    # D never appears anywhere as a user_id: no subscription, wallet, ledger, nor event owner.
    assert await _subscription(db_sessionmaker, _D) is None
    assert await _balance(db_sessionmaker, _D) is None
    assert await _ledger_keys(db_sessionmaker, _D) == []
    assert await _event_user_ids(db_sessionmaker, _TXN) == [str(_U)]

    # resolvedVia="device_id" and the RESOLVED userId (U, not D) are on the outcome log.
    recs = _outcomes(caplog)
    assert len(recs) == 1
    fields = _rendered(recs[0])
    assert fields["resolvedVia"] == "device_id"
    assert fields["userId"] == str(_U)


@pytest.mark.asyncio
async def test_device_resolve_token_package_credits_the_linked_user(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A one-time token package on the device-resolve path: credits U, subscription untouched.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)

    data = {"user_id": str(_D), "product_id": _TOKEN_PRODUCT}
    # Anti-tamper: a huge top-level Amount must NOT size the grant (server-map only).
    outcome = await cp_service.handle(_body(data=data, account_id=str(_D), amount=999_999_999))
    await cp_service._session.commit()
    assert outcome.result == "applied"

    assert await _balance(db_sessionmaker, _U) == _TOKEN_CREDITS  # server map, not the payload
    assert await _ledger_keys(db_sessionmaker, _U) == [f"cp-txn:{_TXN}"]
    assert await _subscription(db_sessionmaker, _U) is None
    assert await _balance(db_sessionmaker, _D) is None


@pytest.mark.asyncio
async def test_device_resolve_anti_tamper_subscription_amount_ignored(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Subscription on the device path with an inflated Amount + recurring_amount -> server map wins.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)

    body = _body(
        data=_sub_data(str(_D), recurring_amount="9999999.00"),
        account_id=str(_D),
        amount=999_999_999,
    )
    outcome = await cp_service.handle(body)
    await cp_service._session.commit()
    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS


@pytest.mark.asyncio
async def test_device_resolve_uppercase_account_id_normalised_and_matched(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # auth_devices stores the lower deviceId; the callback sends it UPPER -> normalised -> matched.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)  # lower canonical
    caplog.set_level(logging.DEBUG)

    upper = str(_D).upper()
    # Data carries no user_id -> AccountId (UPPER) is the sole source; still resolves via device.
    data = {"product_id": _SUB_PRODUCT, "billing_interval_unit": "year"}
    outcome = await cp_service.handle(_body(data=data, account_id=upper))
    await cp_service._session.commit()

    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert _rendered(_outcomes(caplog)[0])["resolvedVia"] == "device_id"


@pytest.mark.asyncio
async def test_device_resolve_idempotent_duplicate_transaction(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A duplicate TransactionId on the device path -> duplicate, U's balance does not grow.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)

    first = await cp_service.handle(_body(data=_sub_data(str(_D)), account_id=str(_D)))
    await cp_service._session.commit()
    assert first.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS

    caplog.set_level(logging.DEBUG)
    second = await cp_service.handle(_body(data=_sub_data(str(_D)), account_id=str(_D)))
    await cp_service._session.commit()
    assert second.result == "duplicate"
    # Still exactly one grant / balance unchanged on the replay.
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == [f"cp-txn:{_TXN}"]
    # duplicate outcome still reports the resolved path (ADR-053: resolvedVia on duplicate).
    assert _rendered(_outcomes(caplog)[0])["resolvedVia"] == "device_id"


# ============================ §1a user-resolve (backward compat) ============================


@pytest.mark.asyncio
async def test_user_resolve_direct_userid_still_works(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # AccountId IS our userId (present in users) -> path (a), resolvedVia="user_id", accrual on U.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    caplog.set_level(logging.DEBUG)

    outcome = await cp_service.handle(_body(data=_sub_data(str(_U)), account_id=str(_U)))
    await cp_service._session.commit()

    assert outcome.result == "applied"
    assert await _subscription(db_sessionmaker, _U) == ("active", _SUB_PRODUCT)
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    fields = _rendered(_outcomes(caplog)[0])
    assert fields["resolvedVia"] == "user_id"
    assert fields["userId"] == str(_U)


@pytest.mark.asyncio
async def test_user_resolve_takes_priority_over_device(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ADR-053 §4: if X is BOTH a users row AND an auth_devices.device_id, users wins (a before b).
    other = uuid.uuid4()
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
        await seed_user(s, user_id=other)
    # Register X=_U as a device pointing at a DIFFERENT user; the users match must still win.
    await _seed_device(db_sessionmaker, device_id=str(_U), user_id=other)
    caplog.set_level(logging.DEBUG)

    outcome = await cp_service.handle(_body(data=_sub_data(str(_U)), account_id=str(_U)))
    await cp_service._session.commit()

    assert outcome.result == "applied"
    assert _rendered(_outcomes(caplog)[0])["resolvedVia"] == "user_id"
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _balance(db_sessionmaker, other) is None  # the device-linked user is NOT credited


# ============================ §1c not found (no provisioning) ============================


@pytest.mark.asyncio
async def test_unresolvable_identifier_is_user_not_found(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A UUID present neither in users nor in auth_devices -> user_not_found (WARNING), no accrual.
    stranger = uuid.uuid4()
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)  # an unrelated user exists but is not the target
    caplog.set_level(logging.DEBUG)

    outcome = await cp_service.handle(
        _body(data=_sub_data(str(stranger)), account_id=str(stranger).upper())
    )
    await cp_service._session.commit()

    assert (outcome.result, outcome.reason) == ("ignored", "user_not_found")
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    # Neither resolvedVia nor userId is present (resolution failed; X is only a candidate).
    assert "resolvedVia" not in fields
    assert "userId" not in fields
    # No event / grant / subscription for anyone.
    assert await _event_user_ids(db_sessionmaker, _TXN) == []
    assert await _balance(db_sessionmaker, stranger) is None
    assert await _balance(db_sessionmaker, _U) is None
    assert await _subscription(db_sessionmaker, _U) is None
