"""Integration: ADR-050 — RU CloudPayments webhook end-to-end against the REAL PostgreSQL container.

Drives the FULL HTTP path ``POST /v1/billing/cloudpayments/webhook`` through the real per-route
bearer auth, the defensive parser and the single-transaction service. Hermetic: the DB is the
shared testcontainers Postgres (seeded via ``seed_user``); the webhook secret + product/token maps
are injected via env + ``get_settings.cache_clear()`` and restored afterwards; no network to
broadapps/YooKassa; no LLM (the RU path never touches an LLM client, so placeholder LLM keys are
irrelevant).

Covers §1 (auth 401/500), §3 (real payload subscription grant + token package), §3b anti-tamper
(Amount in the body never sizes the grant), §4 (idempotency + renewal), §2 None-guard AccountId,
§5 user_not_found, gate/invalid_data, unknown_product. Reason-level assertions that the router
collapses to ``{"code":0}`` are covered by the service-driven test module.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import seed_user

_SECRET = "cloudpayments-webhook-secret-value"  # noqa: S105 - test-only static secret
_URL = "/v1/billing/cloudpayments/webhook"

# Subscription product from the ADR-050 real callback; mapped to a distinct per-tier credit amount.
_SUB_PRODUCT = "yearly_49.99_nottrial"
_SUB_TOKENS = 12000  # cloudpayments_product_tokens[_SUB_PRODUCT] (distinct from the fallback)
_FALLBACK = 1000  # cloudpayments_subscription_tokens_grant
_PRODUCT_TOKENS = json.dumps({_SUB_PRODUCT: _SUB_TOKENS})

# One-time token package (present in the server-side TOKEN_PRODUCTS map — anti-tamper source).
_TOKEN_PRODUCT = "100_tokens_9.99"
_TOKEN_CREDITS = 100
_TOKEN_PRODUCTS = json.dumps({_TOKEN_PRODUCT: _TOKEN_CREDITS})

_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"  # arrives UPPER, our backend userId
_TXN = "31d884c8-000f-5001-8000-1fb75b44e1d9"


def _auth(secret: str = _SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
async def cp_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to the container DB with CLOUDPAYMENTS_* + TOKEN_PRODUCTS configured."""
    from app import deps
    from app.api_gateway import rate_limit
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", _SECRET)
    monkeypatch.setenv("CLOUDPAYMENTS_PRODUCT_TOKENS", _PRODUCT_TOKENS)
    monkeypatch.setenv("CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT", str(_FALLBACK))
    monkeypatch.setenv("TOKEN_PRODUCTS", _TOKEN_PRODUCTS)
    get_settings.cache_clear()

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _allow(**_kwargs: Any) -> bool:
        return True

    orig_other = rate_limit.enforce_other_limits
    rate_limit.enforce_other_limits = _allow  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]
    get_settings.cache_clear()


# --------------------------- payload builders (CloudPayments wire form) ---------------------------


def _body(
    *,
    data: dict[str, Any] | str,
    transaction_id: str | None = _TXN,
    account_id: str | None = _UID_UPPER,
    status: str = "Completed",
    operation_type: str = "Payment",
    amount: int = 3990,
    with_card: bool = True,
) -> bytes:
    """Build a CloudPayments callback: flat PascalCase top-level + ``Data`` as a JSON *string*."""
    body: dict[str, Any] = {
        "Status": status,
        "OperationType": operation_type,
        "Amount": amount,
        "Currency": "RUB",
        "Data": json.dumps(data) if isinstance(data, dict) else data,
    }
    if transaction_id is not None:
        body["TransactionId"] = transaction_id
    if account_id is not None:
        body["AccountId"] = account_id
    if with_card:
        body.update(
            {
                "CardFirstSix": "220024",
                "CardLastFour": "8808",
                "Issuer": "VTB",
                "CardType": "Mir",
            }
        )
    return json.dumps(body).encode()


def _sub_data(product_id: str = _SUB_PRODUCT, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "app_id": "2259dcce-0000-0000-0000-000000000000",
        "user_id": _UID_UPPER,
        "product_id": product_id,
        "billing_interval_unit": "year",
        "billing_interval_count": "1",
        "subscription_id": "f95b318c-0000-0000-0000-000000000000",
        "billing_phase": "regular",
    }
    data.update(extra)
    return data


async def _post(client: AsyncClient, body: bytes, secret: str = _SECRET) -> Any:
    r = await client.post(_URL, content=body, headers=_auth(secret))
    return r


# --------------------------- DB helpers ---------------------------


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


async def _event_rows(maker: async_sessionmaker[AsyncSession], txn: str) -> list[dict[str, Any]]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT transaction_id, user_id, product_id, kind, payload "
                    "FROM cloudpayments_webhook_events WHERE transaction_id=:t"
                ),
                {"t": txn},
            )
        ).all()
    return [
        {
            "transaction_id": r.transaction_id,
            "user_id": str(r.user_id),
            "product_id": r.product_id,
            "kind": r.kind,
            "payload": dict(r.payload),
        }
        for r in rows
    ]


# ============================ §1 Authorization ============================


@pytest.mark.asyncio
async def test_missing_bearer_is_401(cp_client: AsyncClient) -> None:
    r = await cp_client.post(_URL, content=_body(data=_sub_data()))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_bearer_is_401(cp_client: AsyncClient) -> None:
    r = await cp_client.post(_URL, content=_body(data=_sub_data()), headers=_auth("nope"))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unset_secret_is_500_misconfigured(
    cp_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No CLOUDPAYMENTS_WEBHOOK_TOKEN => 500 (auth reads get_settings() fresh per request).
    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", "")
    get_settings.cache_clear()
    r = await cp_client.post(_URL, content=_body(data=_sub_data()), headers=_auth("anything"))
    assert r.status_code == 500


# ==================== §3a Happy-path subscription (real payload) ====================


@pytest.mark.asyncio
async def test_real_subscription_payload_grants_once(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)  # normalized-lower UUID pre-registered

    r = await _post(cp_client, _body(data=_sub_data()))
    assert r.status_code == 200 and r.json() == {"code": 0}

    sub = await _subscription(db_sessionmaker, uid)
    assert sub is not None
    status, plan, expires_at = sub
    assert status == "active"
    assert plan == _SUB_PRODUCT
    # expires_at strictly in the future, ~ now + 365d (timedelta approximation, ADR-050 §3a).
    now = __import__("datetime").datetime.now(tz=__import__("datetime").UTC)
    delta_days = (expires_at - now).days
    assert 360 <= delta_days <= 366, delta_days

    # EXACTLY ONE grant keyed by cp-txn:{TransactionId}; amount from the server map (NOT Amount).
    assert await _ledger_keys(db_sessionmaker, uid) == [f"cp-txn:{_TXN}"]
    assert await _balance(db_sessionmaker, uid) == _SUB_TOKENS


@pytest.mark.asyncio
async def test_subscription_unmapped_product_uses_fallback(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    # A subscription id NOT in CLOUDPAYMENTS_PRODUCT_TOKENS but classified by interval => fallback.
    r = await _post(cp_client, _body(data=_sub_data(product_id="monthly_1.99_nottrial")))
    assert r.status_code == 200
    assert await _balance(db_sessionmaker, uid) == _FALLBACK


# ============================ §3b Happy-path token package ============================


@pytest.mark.asyncio
async def test_token_package_one_time_grant_subscription_untouched(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    # Token package: product_id in TOKEN_PRODUCTS, no billing interval.
    data = {"user_id": _UID_UPPER, "product_id": _TOKEN_PRODUCT}
    r = await _post(cp_client, _body(data=data))
    assert r.status_code == 200

    # Exactly N credits from the server-side TOKEN_PRODUCTS map; subscription NOT created.
    assert await _balance(db_sessionmaker, uid) == _TOKEN_CREDITS
    assert await _ledger_keys(db_sessionmaker, uid) == [f"cp-txn:{_TXN}"]
    assert await _subscription(db_sessionmaker, uid) is None


# ============================ §3b Anti-tamper ============================


@pytest.mark.asyncio
async def test_anti_tamper_amount_does_not_size_grant(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    # Huge top-level Amount + huge recurring_amount inside Data: the grant must stay = server map.
    body = _body(data=_sub_data(recurring_amount="9999999.00"), amount=999_999_999)
    r = await _post(cp_client, body)
    assert r.status_code == 200
    assert await _balance(db_sessionmaker, uid) == _SUB_TOKENS  # server card, not the payload


# ============================ §4 Idempotency / renewal ============================


@pytest.mark.asyncio
async def test_duplicate_transaction_id_no_double_grant(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)

    first = await _post(cp_client, _body(data=_sub_data()))
    second = await _post(cp_client, _body(data=_sub_data()))  # identical TransactionId
    assert (first.status_code, second.status_code) == (200, 200)

    # Single grant, single event row — balance unchanged on the replay.
    assert await _balance(db_sessionmaker, uid) == _SUB_TOKENS
    assert await _ledger_keys(db_sessionmaker, uid) == [f"cp-txn:{_TXN}"]
    assert len(await _event_rows(db_sessionmaker, _TXN)) == 1


@pytest.mark.asyncio
async def test_renewal_new_transaction_id_grants_again(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    txn2 = "42e995d9-111f-6112-9111-2fc86c55f2ea"

    # Period 1, then a renewal: same subscription_id, NEW TransactionId => a fresh grant.
    await _post(cp_client, _body(data=_sub_data(), transaction_id=_TXN))
    await _post(cp_client, _body(data=_sub_data(), transaction_id=txn2))

    keys = await _ledger_keys(db_sessionmaker, uid)
    assert set(keys) == {f"cp-txn:{_TXN}", f"cp-txn:{txn2}"}
    assert await _balance(db_sessionmaker, uid) == 2 * _SUB_TOKENS


# ============================ §2 None-guard userId ============================


@pytest.mark.asyncio
async def test_upper_case_account_id_is_normalised_and_found(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # AccountId arrives UPPER; the user is seeded under the lower canonical UUID and IS matched.
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    # Data carries no user_id -> AccountId (UPPER) is the sole source; must still resolve.
    data = {"product_id": _SUB_PRODUCT, "billing_interval_unit": "year"}
    r = await _post(cp_client, _body(data=data, account_id=_UID_UPPER))
    assert r.status_code == 200
    assert await _balance(db_sessionmaker, uid) == _SUB_TOKENS


@pytest.mark.asyncio
async def test_missing_account_id_and_user_id_is_ignored_not_500(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Neither AccountId nor Data.user_id -> ignored/invalid_account_id, HTTP 200 (NOT 500), no rows.
    data = {"product_id": _SUB_PRODUCT, "billing_interval_unit": "year"}
    r = await _post(cp_client, _body(data=data, account_id=None))
    assert r.status_code == 200 and r.json() == {"code": 0}
    assert await _event_rows(db_sessionmaker, _TXN) == []


@pytest.mark.asyncio
async def test_non_uuid_account_id_is_ignored(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    data = {"product_id": _SUB_PRODUCT, "billing_interval_unit": "year"}
    r = await _post(cp_client, _body(data=data, account_id="not-a-uuid"))
    assert r.status_code == 200
    assert await _event_rows(db_sessionmaker, _TXN) == []


# ============================ §5 user_not_found ============================


@pytest.mark.asyncio
async def test_user_not_found_is_ignored_no_event_no_grant(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A valid UUID with no users row: 200, no event recorded, no grant (client must send our uid).
    uid = uuid.uuid4()
    data = {"product_id": _SUB_PRODUCT, "billing_interval_unit": "year"}
    r = await _post(cp_client, _body(data=data, account_id=str(uid).upper()))
    assert r.status_code == 200 and r.json() == {"code": 0}
    assert await _event_rows(db_sessionmaker, _TXN) == []
    assert await _balance(db_sessionmaker, uid) is None
    assert await _subscription(db_sessionmaker, uid) is None


# ============================ §2 Gate / invalid_data / unknown product ============================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, op",
    [("Pending", "Payment"), ("Completed", "Refund"), ("Declined", "Payment")],
)
async def test_gate_non_completed_payment_is_ignored(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    status: str,
    op: str,
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    r = await _post(cp_client, _body(data=_sub_data(), status=status, operation_type=op))
    assert r.status_code == 200
    assert await _balance(db_sessionmaker, uid) is None
    assert await _event_rows(db_sessionmaker, _TXN) == []


@pytest.mark.asyncio
async def test_malformed_data_string_is_ignored_not_500(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    r = await _post(cp_client, _body(data="{not valid json"))
    assert r.status_code == 200 and r.json() == {"code": 0}
    assert await _event_rows(db_sessionmaker, _TXN) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("product_id", ["randomsku", "999_tokens_pack"])
async def test_unknown_product_no_event_no_grant(
    cp_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    product_id: str,
) -> None:
    # "randomsku" -> unknown by classify; "999_tokens_pack" -> token-name but absent from
    # TOKEN_PRODUCTS -> anti-tamper unknown_product. Neither records an event nor grants.
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    data = {"user_id": _UID_UPPER, "product_id": product_id}
    r = await _post(cp_client, _body(data=data))
    assert r.status_code == 200
    assert await _balance(db_sessionmaker, uid) is None
    assert await _event_rows(db_sessionmaker, _TXN) == []


# ============================ §7 Persisted payload carries NO card PII ============================


@pytest.mark.asyncio
async def test_persisted_event_payload_has_no_card_data(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    r = await _post(cp_client, _body(data=_sub_data(), with_card=True))
    assert r.status_code == 200

    rows = await _event_rows(db_sessionmaker, _TXN)
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == str(uid)
    assert row["product_id"] == _SUB_PRODUCT
    assert row["kind"] == "subscription"
    payload = row["payload"]
    blob = json.dumps(payload)
    for forbidden in ("CardFirstSix", "CardLastFour", "Issuer", "CardType", "220024", "8808"):
        assert forbidden not in blob
    # Only the sanitized allowlist keys are stored.
    assert set(payload) == {
        "transactionId",
        "productId",
        "kind",
        "status",
        "operationType",
        "amount",
        "currency",
        "testMode",
        "billingIntervalUnit",
        "billingIntervalCount",
        "billingPhase",
        "subscriptionId",
    }
