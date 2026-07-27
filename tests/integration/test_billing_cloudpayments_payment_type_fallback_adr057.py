"""Integration: ADR-057 — provider misreports a subscription product as ``one_time``.

Regression cover for the avelyra incident of 2026-07-19: broadapps returned
``product.payment_type = "one_time"`` for ``week_6.99_nottrial`` (a weekly SUBSCRIPTION whose
checkout link our own allowlist had issued). The webhook classified it as a token purchase, found
no entry in ``TOKEN_PRODUCTS``, and silently dropped a ``succeeded`` payment — the tester paid and
received nothing.

Two behaviours are pinned here:

1. **The fallback** (ADR-057 §3): a ``one_time`` payment whose ``product.code`` is NOT a configured
   token product is re-classified with ``parser.classify_product`` — the SAME rule
   ``checkout.validate_product`` used to issue the link. ``subscription`` -> credit as a
   subscription + WARNING ``cloudpayments_payment_type_mismatch``. Anything else -> skip as before,
   so no path that credits today changes.
2. **Skip is not duplicate** (ADR-057 §4): a dropped PAID payment surfaces as
   ``ignored/payment_skipped`` at WARNING, not as the benign ``duplicate`` it used to be
   indistinguishable from.

Same harness as ``test_billing_cloudpayments_verification_adr054.py``: real testcontainers Postgres,
fake verify client, no network.
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
_SKIPPED = "cloudpayments_payment_skipped"
_MISMATCH = "cloudpayments_payment_type_mismatch"
_LOGGER = "app.billing_cloudpayments.service"

# The incident product: looks like a subscription by code, priced per week.
_SUB_CODE = "week_6.99_nottrial"
_SUB_TOKENS = 12000  # per-tier value from CLOUDPAYMENTS_PRODUCT_TOKENS
_FALLBACK = 1000  # CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT
_TOKEN_CODE = "100_tokens_9.99"
_TOKEN_CREDITS = 100
# Looks like a token product by name but is absent from TOKEN_PRODUCTS -> must STAY skipped
# (never guess an amount for a consumable — ADR-054 §6 anti-tamper).
_UNCONFIGURED_TOKEN_CODE = "999_tokens_pack"

_U = uuid.UUID("b0f407bd-4a19-449e-beab-84ce341d6915")


class FakeVerifyClient:
    """Scripted stand-in for ``CloudPaymentsVerifyClient`` (no network)."""

    def __init__(self, *, payments: list[dict[str, Any]] | None = None) -> None:
        self._payments = payments if payments is not None else []
        self.calls: list[uuid.UUID] = []

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        self.calls.append(device_id)
        return [dict(p) for p in self._payments]


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _payment(
    payment_id: str, *, code: str, payment_type: str, status: str = "succeeded"
) -> dict[str, Any]:
    return {
        "payment_id": payment_id,
        "status": status,
        "paid_at": (_now() - datetime.timedelta(minutes=5)).isoformat(),
        "product": {"code": code, "payment_type": payment_type},
    }


def _settings(**over: Any) -> Settings:
    kwargs: dict[str, Any] = {
        "CLOUDPAYMENTS_API_TOKEN": "verify-token-secret",
        "CLOUDPAYMENTS_PRODUCT_TOKENS": json.dumps({_SUB_CODE: _SUB_TOKENS}),
        "CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT": _FALLBACK,
        "TOKEN_PRODUCTS": json.dumps({_TOKEN_CODE: _TOKEN_CREDITS}),
    }
    kwargs.update(over)
    return Settings(**kwargs)


def _service(
    session: AsyncSession, verify: FakeVerifyClient, settings: Settings | None = None
) -> CloudPaymentsWebhookService:
    audit = AuditService(session)
    return CloudPaymentsWebhookService(
        session, WalletService(session, audit), audit, settings or _settings(), verify
    )


def _body(*, account_id: str) -> bytes:
    return json.dumps(
        {
            "Status": "Completed",
            "OperationType": "Payment",
            "Amount": 100,  # anti-tamper: never sizes the grant
            "Currency": "RUB",
            "AccountId": account_id,
        }
    ).encode()


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int | None:
    async with maker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        return None if bal is None else int(bal)


async def _subscription(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> tuple[str, str | None, Any] | None:
    async with maker() as s:
        row = (
            await s.execute(
                text("SELECT status, plan, expires_at FROM subscriptions WHERE user_id=:u"),
                {"u": str(uid)},
            )
        ).first()
    return None if row is None else (row.status, row.plan, row.expires_at)


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


def _records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == message]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


@pytest.fixture(autouse=True)
def _enable_service_logger() -> None:
    logging.getLogger(_LOGGER).disabled = False


# ==================== §3 The fallback: one_time subscription product ====================


@pytest.mark.asyncio
async def test_one_time_subscription_product_is_credited_as_subscription(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The incident case: paid, misreported as one_time -> credited as a subscription."""
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[_payment("pay-sub", code=_SUB_CODE, payment_type="one_time")]
    )

    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))

    assert outcome.result == "applied"
    # Sized from the per-tier subscription map, NOT the token map and NOT the callback Amount.
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:pay-sub"]
    # A subscription row is what the user actually bought — the pre-ADR-057 tokens branch
    # would never have created one even if the code had been in TOKEN_PRODUCTS.
    sub = await _subscription(db_sessionmaker, _U)
    assert sub is not None
    status, plan, expires_at = sub
    assert (status, plan) == ("active", _SUB_CODE)
    # "week" inferred from the code -> 7 days.
    assert datetime.timedelta(days=6) < (expires_at - _now()) < datetime.timedelta(days=8)
    # The provider misconfiguration is loud, so it can be chased and the fallback retired.
    mismatches = _records(caplog, _MISMATCH)
    assert len(mismatches) == 1
    assert mismatches[0].levelno == logging.WARNING
    rendered = _rendered(mismatches[0])
    assert rendered["productId"] == _SUB_CODE
    assert rendered["paymentType"] == "one_time"
    assert rendered["creditedAs"] == "subscription"
    assert _records(caplog, _SKIPPED) == []


@pytest.mark.asyncio
async def test_fallback_credit_is_idempotent_on_redelivery(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-delivery of a fallback-credited payment is a real duplicate, not a second grant."""
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[_payment("pay-sub", code=_SUB_CODE, payment_type="one_time")]
    )
    service = _service(db_session, verify)

    first = await service.handle(_body(account_id=str(_U)))
    second = await service.handle(_body(account_id=str(_U)))

    assert first.result == "applied"
    assert second.result == "duplicate" and second.reason is None
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:pay-sub"]


@pytest.mark.asyncio
async def test_configured_token_product_is_unaffected_by_the_fallback(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuine one_time token purchase keeps crediting tokens — no re-classification."""
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[_payment("pay-tok", code=_TOKEN_CODE, payment_type="one_time")]
    )

    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))

    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _TOKEN_CREDITS
    assert await _subscription(db_sessionmaker, _U) is None
    assert _records(caplog, _MISMATCH) == []


@pytest.mark.asyncio
async def test_declared_subscription_still_takes_the_direct_path(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once the provider reports payment_type=subscription again, the fallback stops firing."""
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[_payment("pay-sub2", code=_SUB_CODE, payment_type="subscription")]
    )

    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))

    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert _records(caplog, _MISMATCH) == []


@pytest.mark.asyncio
async def test_unconfigured_token_code_is_still_skipped(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fallback never invents a consumable: a token-shaped unknown code stays skipped."""
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[_payment("pay-unk", code=_UNCONFIGURED_TOKEN_CODE, payment_type="one_time")]
    )

    await _service(db_session, verify).handle(_body(account_id=str(_U)))

    assert await _balance(db_sessionmaker, _U) is None
    assert await _subscription(db_sessionmaker, _U) is None
    skips = _records(caplog, _SKIPPED)
    assert len(skips) == 1 and _rendered(skips[0])["reason"] == "unknown_product"
    assert _records(caplog, _MISMATCH) == []


# ==================== §4 A skipped payment is not a duplicate ====================


@pytest.mark.asyncio
async def test_skipped_payment_outcome_is_ignored_not_duplicate(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped PAID payment is an incident: ignored/payment_skipped at WARNING.

    Pre-ADR-057 this logged ``result="duplicate"`` with ``creditedCount: 0`` — the avelyra
    incident looked like a benign re-delivery and no alert could distinguish it.
    """
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[_payment("pay-unk", code=_UNCONFIGURED_TOKEN_CODE, payment_type="one_time")]
    )

    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))

    assert outcome.result == "ignored"
    assert outcome.reason == "payment_skipped"
    outcomes = _records(caplog, _MESSAGE)
    assert len(outcomes) == 1
    assert outcomes[0].levelno == logging.WARNING
    rendered = _rendered(outcomes[0])
    assert rendered["result"] == "ignored"
    assert rendered["reason"] == "payment_skipped"
    assert rendered["creditedCount"] == 0


@pytest.mark.asyncio
async def test_credited_payment_alongside_skipped_still_reports_applied(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mixed batch: one credit wins the aggregate (the skip stays a per-payment WARNING)."""
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[
            _payment("pay-unk", code=_UNCONFIGURED_TOKEN_CODE, payment_type="one_time"),
            _payment("pay-tok", code=_TOKEN_CODE, payment_type="one_time"),
        ]
    )

    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))

    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _TOKEN_CREDITS
    assert len(_records(caplog, _SKIPPED)) == 1
