"""Integration: одна оплата Apple — одно начисление (ADR-106 §A–§E) на реальной БД.

Сценарии — ``docs/modules/{billing-adapty,subscription,token-purchase,wallet-ledger}/09-testing.md``
§«Одна оплата — одно начисление». Суммы каналов в фикстуре заведомо РАЗНЫЕ
(``SUBSCRIPTION_CREDITS_PER_PERIOD`` 333 ≠ ``ADAPTY_PRODUCT_TOKENS[pid]`` 555 ≠ фолбэк Adapty 222):
на равных суммах конфликт суммы под общим ключом не воспроизводится.

Сервисы вызываются напрямую (как ``test_grant_channel_fallbacks_adr099.py``): отсутствие
исключения у ``sync``/``purchase`` = ``200`` (``ConflictError`` роутер отдал бы ``409``).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import jwt as pyjwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.billing_adapty import parser
from app.billing_adapty.service import AdaptyWebhookService, WebhookOutcome
from app.config import get_settings
from app.instance_config.snapshot import InstanceConfigSnapshot, ProductOverlay, install_snapshot
from app.subscription.service import SubscriptionResult, SubscriptionService
from app.subscription.storekit import StoreKitVerifier, VerifiedTransaction
from app.token_purchase.service import PurchaseResult, TokenPurchaseService
from app.wallet.service import WalletService
from tests.conftest import seed_user

_PERIOD = 333  # SUBSCRIPTION_CREDITS_PER_PERIOD (StoreKit)
_ADAPTY_MAPPED = 555  # ADAPTY_PRODUCT_TOKENS[_SUB]
_ADAPTY_FALLBACK = 222  # ADAPTY_SUBSCRIPTION_TOKENS_GRANT
_SUB = "sub.month"  # в карте Adapty => в каталоге инстанса
_UNKNOWN_SUB = "sub.unknown.month"  # ни в оверлее, ни в картах, ни в каталоге
_PACK = "tokens_100"
_PACK_CREDITS = 100
_TEST_SECRET = "adr106-storekit-test-secret"

_ENV: dict[str, Any] = {
    "SUBSCRIPTION_CREDITS_PER_PERIOD": _PERIOD,
    "ADAPTY_SUBSCRIPTION_TOKENS_GRANT": _ADAPTY_FALLBACK,
    "ADAPTY_PRODUCT_TOKENS": json.dumps({_SUB: _ADAPTY_MAPPED}),
    "CLOUDPAYMENTS_PRODUCT_TOKENS": "{}",
    "TOKEN_PRODUCTS": json.dumps({_PACK: _PACK_CREDITS}),
    "PRODUCTS_CATALOG": "",
    "ADAPTY_WEBHOOK_SECRET": "adapty-secret",
    "STOREKIT_TEST_MODE": "true",
    "STOREKIT_TEST_SECRET": _TEST_SECRET,
    "APPSTORE_BUNDLE_ID": "",
    "APPSTORE_ROOT_CERT_DIR": "",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for alias, value in _ENV.items():
        monkeypatch.setenv(alias, str(value))
    get_settings.cache_clear()
    # Alembic's fileConfig disables loggers created before it ran (see ADR-046 logging tests).
    for name in ("app.billing_adapty.service", "app.subscription.service"):
        logging.getLogger(name).disabled = False
    yield
    get_settings.cache_clear()


def _in(days: float) -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=days)


# ------------------------------------ builders ------------------------------------------------
def _adapty(session: AsyncSession) -> AdaptyWebhookService:
    audit = AuditService(session)
    return AdaptyWebhookService(session, WalletService(session, audit), audit, get_settings())


class _Verifier:
    def __init__(self, txn: VerifiedTransaction) -> None:
        self._txn = txn

    def verify(self, _signed: str) -> VerifiedTransaction:
        return self._txn


def _txn(
    transaction_id: str,
    *,
    product_id: str = _SUB,
    expires_at: datetime.datetime | None = None,
    upgraded: bool = False,
) -> VerifiedTransaction:
    return VerifiedTransaction(
        transaction_id=transaction_id,
        original_transaction_id="orig-1",
        product_id=product_id,
        expires_at=expires_at if expires_at is not None else _in(30),
        revoked=False,
        environment="sandbox",
        upgraded=upgraded,
    )


async def _sync(
    session: AsyncSession, uid: uuid.UUID, txn: VerifiedTransaction
) -> SubscriptionResult:
    audit = AuditService(session)
    service = SubscriptionService(
        session,
        _Verifier(txn),  # type: ignore[arg-type]
        WalletService(session, audit),
        audit,
    )
    return await service.sync(uid, "signed")


async def _purchase(
    session: AsyncSession, uid: uuid.UUID, transaction_id: str, product_id: str = _PACK
) -> PurchaseResult:
    audit = AuditService(session)
    txn = VerifiedTransaction(
        transaction_id=transaction_id,
        original_transaction_id=transaction_id,
        product_id=product_id,
        expires_at=None,
        revoked=False,
        environment="sandbox",
    )
    service = TokenPurchaseService(session, _Verifier(txn), WalletService(session, audit))  # type: ignore[arg-type]
    return await service.purchase(uid, "signed")


def _event(
    *,
    event_id: str,
    event_type: str = "subscription_renewed",
    customer_user_id: uuid.UUID | str | None = None,
    profile_id: uuid.UUID | str | None = None,
    transaction_id: str | None = None,
    original_transaction_id: str | None = None,
    product_id: str | None = _SUB,
    expires_at: datetime.datetime | None = None,
    will_renew: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> bytes:
    ep: dict[str, Any] = {}
    if transaction_id is not None:
        ep["transaction_id"] = transaction_id
    if original_transaction_id is not None:
        ep["original_transaction_id"] = original_transaction_id
    if product_id is not None:
        ep["vendor_product_id"] = product_id
    if expires_at is not None:
        ep["subscription_expires_at"] = expires_at.isoformat()
    if will_renew is not None:
        ep["will_renew"] = will_renew
    if profile_id is not None:
        ep["profile_id"] = str(profile_id)
    ep.update(extra or {})
    body: dict[str, Any] = {
        "profile_event_id": event_id,
        "event_type": event_type,
        "event_properties": ep,
    }
    if customer_user_id is not None:
        body["customer_user_id"] = str(customer_user_id)
    return json.dumps(body).encode()


# ------------------------------------ readers -------------------------------------------------
async def _ledger(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> list[tuple[str, int, str]]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT idempotency_key, amount, type FROM ledger_transactions "
                    "WHERE user_id=:u ORDER BY created_at"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [(r[0], int(r[1]), r[2]) for r in rows]


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        value = await s.scalar(
            text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)}
        )
    return int(value or 0)


async def _subscription(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> tuple[str, str | None, datetime.datetime | None, bool | None] | None:
    async with maker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, plan, expires_at, will_renew FROM subscriptions "
                    "WHERE user_id=:u"
                ),
                {"u": str(uid)},
            )
        ).first()
    return None if row is None else (row[0], row[1], row[2], row[3])


async def _audits(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, event_type: str
) -> list[dict[str, Any]]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM audit_logs WHERE user_id=:u AND event_type=:t "
                    "ORDER BY created_at"
                ),
                {"u": str(uid), "t": event_type},
            )
        ).all()
    return [dict(r[0]) for r in rows]


async def _count(maker: async_sessionmaker[AsyncSession], table: str) -> int:
    async with maker() as s:
        return int(await s.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


async def _seed_ledger(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, key: str, amount: int
) -> None:
    async with maker() as s:
        audit = AuditService(s)
        await WalletService(s, audit).grant(
            user_id=uid, amount=amount, idempotency_key=key, meta={}, reason="historical"
        )
        await s.commit()


async def _handle(maker: async_sessionmaker[AsyncSession], raw: bytes) -> WebhookOutcome:
    async with maker() as s:
        outcome = await _adapty(s).handle(raw)
        await s.commit()
    return outcome


def _records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == message]


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    return dict(record.__dict__.get("extra_fields", {}))


_APPLIED = WebhookOutcome(result="applied")


# ================================= A. один период — один грант =================================
@pytest.mark.asyncio
async def test_sync_then_webhook_same_transaction_credits_once(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _sync(s, uid, _txn("T-1"))
        await s.commit()

    outcome = await _handle(
        db_sessionmaker, _event(event_id="e-1", customer_user_id=uid, transaction_id="T-1")
    )

    assert outcome == _APPLIED
    assert await _ledger(db_sessionmaker, uid) == [("sub-grant:T-1", _PERIOD, "credit")]


@pytest.mark.asyncio
async def test_webhook_then_sync_credits_once_with_the_first_channel_amount(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    assert (
        await _handle(
            db_sessionmaker,
            _event(event_id="e-1", customer_user_id=uid, transaction_id="T-1", expires_at=_in(30)),
        )
        == _APPLIED
    )

    async with db_sessionmaker() as s:
        result = await _sync(s, uid, _txn("T-1"))
        await s.commit()

    assert result.is_subscribed is True
    assert await _ledger(db_sessionmaker, uid) == [("sub-grant:T-1", _ADAPTY_MAPPED, "credit")]
    assert await _balance(db_sessionmaker, uid) == _ADAPTY_MAPPED


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["webhook", "sync"])
async def test_race_of_both_channels_with_different_amounts_is_not_an_error(
    db_sessionmaker: async_sessionmaker[AsyncSession], first: str
) -> None:
    """§A3: обе проверки §A2 проходят до записи; второй INSERT встаёт на уникальном ключе.

    Первый канал держит строку незакоммиченной, второй успевает проверить ключи (строки не видно)
    и блокируется на записи; после коммита первого второй получает ``ON CONFLICT DO NOTHING`` с
    ДРУГОЙ суммой — ровно ветка ``ConflictError``. Подписка засеяна с более поздним сроком, чтобы
    ни один канал не писал строку ``subscriptions`` (иначе второй заблокировался бы ДО проверки
    ключей и ветка конфликта не воспроизвелась бы).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", expires_in_hours=24 * 60, balance=0)
    raw = _event(event_id="e-race", customer_user_id=uid, transaction_id="T-R", expires_at=_in(30))

    first_session = db_sessionmaker()
    second_session = db_sessionmaker()
    try:
        if first == "webhook":
            assert await _adapty(first_session).handle(raw) == _APPLIED
            second = asyncio.create_task(_sync(second_session, uid, _txn("T-R")))
        else:
            await _sync(first_session, uid, _txn("T-R"))
            second = asyncio.create_task(_adapty(second_session).handle(raw))
        await asyncio.sleep(0.5)
        assert not second.done()  # blocked on the unique key of the uncommitted first row
        await first_session.commit()
        second_result = await second
        await second_session.commit()
    finally:
        await first_session.close()
        await second_session.close()

    if first == "sync":
        assert second_result == _APPLIED
    first_amount = _ADAPTY_MAPPED if first == "webhook" else _PERIOD
    assert await _ledger(db_sessionmaker, uid) == [("sub-grant:T-R", first_amount, "credit")]
    assert await _balance(db_sessionmaker, uid) == first_amount


@pytest.mark.asyncio
async def test_historical_adapty_txn_row_blocks_both_channels(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    await _seed_ledger(db_sessionmaker, uid, "adapty-txn:T-H", _ADAPTY_MAPPED)

    outcome = await _handle(
        db_sessionmaker, _event(event_id="e-h", customer_user_id=uid, transaction_id="T-H")
    )
    async with db_sessionmaker() as s:
        await _sync(s, uid, _txn("T-H"))
        await s.commit()

    assert outcome == _APPLIED
    assert await _ledger(db_sessionmaker, uid) == [("adapty-txn:T-H", _ADAPTY_MAPPED, "credit")]


@pytest.mark.asyncio
async def test_existing_sub_grant_row_with_other_amount_blocks_webhook_and_sync(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    await _seed_ledger(db_sessionmaker, uid, "sub-grant:T-S", 7)

    outcome = await _handle(
        db_sessionmaker, _event(event_id="e-s", customer_user_id=uid, transaction_id="T-S")
    )
    async with db_sessionmaker() as s:
        result = await _sync(s, uid, _txn("T-S"))  # no ConflictError => 200, not 409
        await s.commit()

    assert outcome == _APPLIED
    assert result.is_subscribed is True
    assert await _ledger(db_sessionmaker, uid) == [("sub-grant:T-S", 7, "credit")]


@pytest.mark.asyncio
async def test_renewal_new_transaction_grants_again_and_fallback_key_is_unchanged(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    await _handle(
        db_sessionmaker,
        _event(event_id="e-1", customer_user_id=uid, transaction_id="T-1", expires_at=_in(30)),
    )
    async with db_sessionmaker() as s:
        await _sync(s, uid, _txn("T-2", expires_at=_in(60)))
        await s.commit()
    # Same Apple transaction delivered as two granting events (distinct profile_event_id).
    await _handle(
        db_sessionmaker,
        _event(
            event_id="e-2b",
            event_type="access_level_updated",
            customer_user_id=uid,
            transaction_id="T-2",
            expires_at=_in(60),
            extra={"is_active": True, "access_level_id": "premium"},
        ),
    )
    # No transaction_id: the ADR-047 fallback key stays adapty-txn:{original_transaction_id}.
    await _handle(
        db_sessionmaker,
        _event(
            event_id="e-3",
            customer_user_id=uid,
            original_transaction_id="ORIG-9",
            expires_at=_in(90),
        ),
    )

    assert sorted(k for k, _, _ in await _ledger(db_sessionmaker, uid)) == [
        "adapty-txn:ORIG-9",
        "sub-grant:T-1",
        "sub-grant:T-2",
    ]


# ================================= B. резолв по profile_id =====================================
def test_parse_profile_id_sources_and_validation() -> None:
    pid = uuid.uuid4()
    assert parser.parse_profile_id({"profile_id": str(pid)}) == pid
    assert parser.parse_profile_id({"profile": {"profile_id": str(pid)}}) == pid
    assert parser.parse_profile_id({"event_properties": {"profile_id": str(pid)}}) == pid
    assert parser.parse_profile_id({"event_properties": {"profile_id": "not-a-uuid"}}) is None
    assert parser.parse_profile_id({}) is None


async def _link_device(maker: async_sessionmaker[AsyncSession], device_id: str) -> uuid.UUID:
    async with maker() as s:
        uid = await seed_user(s)
        await s.execute(
            text("INSERT INTO auth_devices (device_id, user_id) VALUES (:d, :u)"),
            {"d": device_id, "u": str(uid)},
        )
        await s.commit()
    return uid


@pytest.mark.asyncio
async def test_profile_id_only_resolves_via_device_in_other_case(
    db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    pid = uuid.uuid4()
    uid = await _link_device(db_sessionmaker, str(pid).upper())

    outcome = await _handle(
        db_sessionmaker, _event(event_id="e-p", profile_id=pid, transaction_id="T-P")
    )

    assert outcome == _APPLIED
    assert await _ledger(db_sessionmaker, uid) == [("sub-grant:T-P", _ADAPTY_MAPPED, "credit")]
    fields = _fields(_records(caplog, "adapty_webhook_outcome")[-1])
    assert fields["resolvedFrom"] == "profile_id"
    assert fields["profileId"] == str(pid)
    assert fields["customerUserId"] is None
    audit = (await _audits(db_sessionmaker, uid, "adapty_subscription"))[-1]
    assert audit["resolvedFrom"] == "profile_id"
    assert audit["customerId"] == str(pid)


@pytest.mark.asyncio
async def test_customer_user_id_wins_when_both_resolve_to_different_users(
    db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    pid = uuid.uuid4()
    other = await _link_device(db_sessionmaker, str(pid))
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    await _handle(
        db_sessionmaker,
        _event(event_id="e-b", customer_user_id=uid, profile_id=pid, transaction_id="T-B"),
    )

    assert len(await _ledger(db_sessionmaker, uid)) == 1
    assert await _ledger(db_sessionmaker, other) == []
    fields = _fields(_records(caplog, "adapty_webhook_outcome")[-1])
    assert fields["resolvedFrom"] == "customer_user_id"
    assert fields["profileId"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("customer_user_id", [uuid.uuid4(), "not-a-uuid"])
async def test_unresolved_or_invalid_customer_user_id_falls_through_to_profile_id(
    db_sessionmaker: async_sessionmaker[AsyncSession], customer_user_id: uuid.UUID | str
) -> None:
    pid = uuid.uuid4()
    uid = await _link_device(db_sessionmaker, str(pid))

    outcome = await _handle(
        db_sessionmaker,
        _event(
            event_id="e-f",
            customer_user_id=customer_user_id,
            profile_id=pid,
            transaction_id="T-F",
        ),
    )

    assert outcome == _APPLIED
    assert len(await _ledger(db_sessionmaker, uid)) == 1


@pytest.mark.asyncio
async def test_no_identifier_vs_unresolved_identifiers(
    db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    pid = uuid.uuid4()

    none = await _handle(db_sessionmaker, _event(event_id="e-0", transaction_id="T-0"))
    both = await _handle(
        db_sessionmaker,
        _event(event_id="e-2", customer_user_id=uuid.uuid4(), profile_id=pid, transaction_id="T"),
    )

    assert none == WebhookOutcome(result="ignored", reason="missing_customer_user_id")
    assert both == WebhookOutcome(result="ignored", reason="user_not_found")
    record = _records(caplog, "adapty_webhook_outcome")[-1]
    assert record.levelno == logging.WARNING
    assert _fields(record)["profileId"] == str(pid)
    assert await _count(db_sessionmaker, "adapty_webhook_events") == 0


# ================================= C. устаревшее событие =======================================
@pytest.mark.asyncio
async def test_stale_granting_keeps_the_row_and_grants_by_period_key(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", expires_in_hours=24 * 60)
    before = await _subscription(db_sessionmaker, uid)

    stale_new = _event(
        event_id="e-st1", customer_user_id=uid, transaction_id="T-OLD", expires_at=_in(30)
    )
    assert await _handle(db_sessionmaker, stale_new) == _APPLIED
    await _seed_ledger(db_sessionmaker, uid, "sub-grant:T-DONE", 1)
    stale_done = _event(
        event_id="e-st2", customer_user_id=uid, transaction_id="T-DONE", expires_at=_in(30)
    )
    assert await _handle(db_sessionmaker, stale_done) == _APPLIED

    assert await _subscription(db_sessionmaker, uid) == before
    assert sorted(await _ledger(db_sessionmaker, uid)) == [
        ("sub-grant:T-DONE", 1, "credit"),
        ("sub-grant:T-OLD", _ADAPTY_MAPPED, "credit"),
    ]
    audits = await _audits(db_sessionmaker, uid, "adapty_subscription")
    assert [a["stale"] for a in audits] == [True, True]


@pytest.mark.asyncio
async def test_stale_expiring_and_noop_do_not_touch_the_row(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", expires_in_hours=24 * 60)
        await s.execute(
            text("UPDATE subscriptions SET will_renew=true WHERE user_id=:u"), {"u": str(uid)}
        )
        await s.commit()
    before = await _subscription(db_sessionmaker, uid)

    await _handle(
        db_sessionmaker,
        _event(
            event_id="e-exp",
            event_type="subscription_expired",
            customer_user_id=uid,
            expires_at=_in(-1),
        ),
    )
    await _handle(
        db_sessionmaker,
        _event(
            event_id="e-noop",
            event_type="subscription_renewal_cancelled",
            customer_user_id=uid,
            expires_at=_in(-1),
            will_renew=False,
        ),
    )

    assert await _subscription(db_sessionmaker, uid) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [90, 60])
async def test_later_or_equal_period_is_not_stale(
    db_sessionmaker: async_sessionmaker[AsyncSession], days: int
) -> None:
    """(б) против переоценки: продление (>) и повтор периода (=) обрабатываются как прежде."""
    expires = _in(60)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active")
        await s.execute(
            text("UPDATE subscriptions SET expires_at=:e WHERE user_id=:u"),
            {"e": expires, "u": str(uid)},
        )
        await s.commit()
    incoming = expires if days == 60 else _in(days)

    await _handle(
        db_sessionmaker,
        _event(event_id="e-l", customer_user_id=uid, transaction_id="T-L", expires_at=incoming),
    )

    row = await _subscription(db_sessionmaker, uid)
    assert row is not None and row[1] == _SUB and row[2] == incoming
    audit = (await _audits(db_sessionmaker, uid, "adapty_subscription"))[-1]
    assert audit["stale"] is False


@pytest.mark.asyncio
async def test_expired_row_is_updated_by_an_earlier_but_future_period(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="expired", expires_in_hours=24 * 60)

    await _handle(
        db_sessionmaker,
        _event(event_id="e-x", customer_user_id=uid, transaction_id="T-X", expires_at=_in(30)),
    )

    row = await _subscription(db_sessionmaker, uid)
    assert row is not None and row[0] == "active" and row[1] == _SUB


@pytest.mark.asyncio
async def test_sync_stale_transaction_answers_from_the_row(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", expires_in_hours=24 * 60)
    before = await _subscription(db_sessionmaker, uid)
    assert before is not None

    async with db_sessionmaker() as s:
        result = await _sync(s, uid, _txn("T-OLD", product_id="other.plan", expires_at=_in(30)))
        await s.commit()

    assert await _subscription(db_sessionmaker, uid) == before
    assert (result.is_subscribed, result.plan, result.expires_at) == (True, before[1], before[2])
    assert await _ledger(db_sessionmaker, uid) == [("sub-grant:T-OLD", _PERIOD, "credit")]
    audit = (await _audits(db_sessionmaker, uid, "subscription_change"))[-1]
    assert audit["stale"] is True

    async with db_sessionmaker() as s:
        await _sync(s, uid, _txn("T-OLD", product_id="other.plan", expires_at=_in(30)))
        await s.commit()
    assert len(await _ledger(db_sessionmaker, uid)) == 1


@pytest.mark.asyncio
async def test_sync_later_transaction_updates_the_row(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", expires_in_hours=24)
    expires = _in(30)

    async with db_sessionmaker() as s:
        result = await _sync(s, uid, _txn("T-NEW", expires_at=expires))
        await s.commit()

    assert result.expires_at == expires
    row = await _subscription(db_sessionmaker, uid)
    assert row is not None and row[1] == _SUB and row[2] == expires


def _signed(*, upgraded: bool | None) -> str:
    payload: dict[str, Any] = {
        "transactionId": "T-UP",
        "originalTransactionId": "T-UP",
        "productId": _SUB,
        "environment": "Sandbox",
        "expiresDate": int(_in(30).timestamp() * 1000),
    }
    if upgraded is not None:
        payload["isUpgraded"] = upgraded
    return pyjwt.encode(payload, _TEST_SECRET, algorithm="HS256")


@pytest.mark.asyncio
async def test_upgraded_transaction_is_inactive_and_not_granted(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    verifier = StoreKitVerifier()  # real HS256 test-mode branch -> real _normalize_payload
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        audit = AuditService(s)
        service = SubscriptionService(s, verifier, WalletService(s, audit), audit)
        upgraded = await service.sync(uid, _signed(upgraded=True))
        await s.commit()

    assert upgraded.is_subscribed is False
    assert await _ledger(db_sessionmaker, uid) == []
    assert (await _audits(db_sessionmaker, uid, "subscription_change"))[-1]["upgraded"] is True

    async with db_sessionmaker() as s:
        audit = AuditService(s)
        service = SubscriptionService(s, verifier, WalletService(s, audit), audit)
        plain = await service.sync(uid, _signed(upgraded=None))
        await s.commit()
    assert plain.is_subscribed is True


# ================================= D. незаведённый продукт =====================================
@pytest.mark.asyncio
async def test_adapty_unmapped_product_warns_once_and_still_grants(
    db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    raw = _event(
        event_id="e-u", customer_user_id=uid, transaction_id="T-U", product_id=_UNKNOWN_SUB
    )

    await _handle(db_sessionmaker, raw)
    await _handle(  # same T, another event: no grant => no signal
        db_sessionmaker,
        _event(
            event_id="e-u2", customer_user_id=uid, transaction_id="T-U", product_id=_UNKNOWN_SUB
        ),
    )
    await _handle(
        db_sessionmaker, _event(event_id="e-m", customer_user_id=uid, transaction_id="T-M")
    )

    assert await _balance(db_sessionmaker, uid) == _ADAPTY_FALLBACK + _ADAPTY_MAPPED
    records = _records(caplog, "subscription_product_unmapped")
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    expected = {
        "channel": "adapty",
        "productId": _UNKNOWN_SUB,
        "amount": _ADAPTY_FALLBACK,
        "transactionId": "T-U",
    }
    assert expected.items() <= _fields(records[0]).items()
    audits = await _audits(db_sessionmaker, uid, "subscription_product_unmapped")
    assert len(audits) == 1 and expected.items() <= audits[0].items()


@pytest.mark.asyncio
async def test_storekit_unmapped_only_for_a_product_outside_the_catalog(
    db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _sync(s, uid, _txn("T-K", product_id=_UNKNOWN_SUB))
        await _sync(s, uid, _txn("T-K", product_id=_UNKNOWN_SUB))  # repeat: no grant, no signal
        await _sync(s, uid, _txn("T-C", product_id=_SUB, expires_at=_in(31)))  # env-map product
        await s.commit()

    assert await _balance(db_sessionmaker, uid) == 2 * _PERIOD
    records = _records(caplog, "subscription_product_unmapped")
    assert len(records) == 1
    assert _fields(records[0])["channel"] == "storekit"
    assert _fields(records[0])["productId"] == _UNKNOWN_SUB
    assert len(await _audits(db_sessionmaker, uid, "subscription_product_unmapped")) == 1


# ================================= E. non_subscription_purchase ================================
def _purchase_event(
    uid: uuid.UUID,
    *,
    event_id: str = "e-pack",
    transaction_id: str | None = "P-1",
    product_id: str | None = _PACK,
    extra: dict[str, Any] | None = None,
) -> bytes:
    return _event(
        event_id=event_id,
        event_type="non_subscription_purchase",
        customer_user_id=uid,
        transaction_id=transaction_id,
        product_id=product_id,
        extra=extra,
    )


@pytest.mark.asyncio
async def test_pack_is_credited_without_subscription_and_ignores_payload_amount(
    db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    raw = _purchase_event(uid, extra={"price_usd": 999, "quantity": 50, "tokens": 100000})

    assert await _handle(db_sessionmaker, raw) == _APPLIED
    assert await _handle(db_sessionmaker, raw) == WebhookOutcome(result="duplicate")

    assert await _ledger(db_sessionmaker, uid) == [("token-purchase:P-1", _PACK_CREDITS, "credit")]
    assert await _subscription(db_sessionmaker, uid) is None
    audit = (await _audits(db_sessionmaker, uid, "adapty_subscription"))[-1]
    assert audit["semantics"] == "one_time_purchase"
    assert (audit["productId"], audit["transactionId"]) == (_PACK, "P-1")
    applied = _fields(_records(caplog, "adapty_webhook_outcome")[0])
    assert (applied["productId"], applied["transactionId"]) == (_PACK, "P-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transaction_id", "product_id", "reason"),
    [
        (None, _PACK, "missing_transaction_id"),
        ("P-2", "tokens_unknown", "unknown_product"),
        ("P-3", _SUB, "unknown_product"),
        ("P-4", None, "unknown_product"),
    ],
)
async def test_pack_refusals_warn_and_write_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    transaction_id: str | None,
    product_id: str | None,
    reason: str,
) -> None:
    caplog.set_level(logging.INFO)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=5)

    outcome = await _handle(
        db_sessionmaker, _purchase_event(uid, transaction_id=transaction_id, product_id=product_id)
    )

    assert outcome == WebhookOutcome(result="ignored", reason=reason)
    record = _records(caplog, "adapty_webhook_outcome")[-1]
    assert record.levelno == logging.WARNING
    assert _fields(record)["transactionId"] == transaction_id
    assert _fields(record)["productId"] == product_id
    assert await _count(db_sessionmaker, "adapty_webhook_events") == 0
    assert await _ledger(db_sessionmaker, uid) == []
    assert await _balance(db_sessionmaker, uid) == 5


@pytest.mark.asyncio
async def test_webhook_then_app_purchase_credits_once(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    await _handle(db_sessionmaker, _purchase_event(uid))

    async with db_sessionmaker() as s:
        result = await _purchase(s, uid, "P-1")
        await s.commit()

    assert (result.credits_added, result.new_balance) == (0, _PACK_CREDITS)
    assert len(await _ledger(db_sessionmaker, uid)) == 1


@pytest.mark.asyncio
async def test_app_purchase_then_webhook_credits_once(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
        await _purchase(s, uid, "P-1")
        await s.commit()

    assert await _handle(db_sessionmaker, _purchase_event(uid)) == _APPLIED
    assert len(await _ledger(db_sessionmaker, uid)) == 1
    assert await _balance(db_sessionmaker, uid) == _PACK_CREDITS


@pytest.mark.asyncio
async def test_app_purchase_racing_webhook_with_changed_overlay_is_not_409(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Строка под ``token-purchase:{T}`` с ДРУГОЙ суммой → ``creditsAdded=0``, не ``409`` (§A3)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    first_session = db_sessionmaker()
    second_session = db_sessionmaker()
    try:
        assert await _adapty(first_session).handle(_purchase_event(uid)) == _APPLIED
        install_snapshot(
            InstanceConfigSnapshot(
                products={
                    _PACK: ProductOverlay(
                        product_id=_PACK,
                        name="Пакет",
                        purchase_kind="one_time",
                        tokens=150,
                        archived=False,
                        updated_at=_in(0),
                    )
                }
            )
        )
        second = asyncio.create_task(_purchase(second_session, uid, "P-1"))
        await asyncio.sleep(0.5)
        assert not second.done()
        await first_session.commit()
        result = await second
        await second_session.commit()
    finally:
        await first_session.close()
        await second_session.close()

    assert result.credits_added == 0
    assert await _ledger(db_sessionmaker, uid) == [("token-purchase:P-1", _PACK_CREDITS, "credit")]


@pytest.mark.asyncio
async def test_unsubscribed_user_gets_pack_by_webhook_and_app_path_keeps_403(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app.errors import SubscriptionRequiredError

    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    await _handle(db_sessionmaker, _purchase_event(uid))

    async with db_sessionmaker() as s:
        with pytest.raises(SubscriptionRequiredError):
            await _purchase(s, uid, "P-1")

    assert await _balance(db_sessionmaker, uid) == _PACK_CREDITS


@pytest.mark.asyncio
async def test_refund_event_is_still_ignored_with_type_echo(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=_PACK_CREDITS)

    outcome = await _handle(
        db_sessionmaker,
        _event(
            event_id="e-ref",
            event_type="non_subscription_purchase_refunded",
            customer_user_id=uid,
            transaction_id="P-1",
            product_id=_PACK,
        ),
    )

    assert outcome == WebhookOutcome(
        result="ignored", event_type="non_subscription_purchase_refunded"
    )
    assert await _balance(db_sessionmaker, uid) == _PACK_CREDITS


# ================================= wallet-ledger ==============================================
@pytest.mark.asyncio
async def test_has_idempotency_key_is_per_user(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        other = await seed_user(s)
    await _seed_ledger(db_sessionmaker, uid, "sub-grant:T-W", 10)

    async with db_sessionmaker() as s:
        wallet = WalletService(s, AuditService(s))
        assert await wallet.has_idempotency_key(uid, "sub-grant:T-W") is True
        assert await wallet.has_idempotency_key(uid, "sub-grant:T-missing") is False
        assert await wallet.has_idempotency_key(other, "sub-grant:T-W") is False
