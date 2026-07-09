"""Integration: ADR-055 — two-step userId resolution in the Adapty webhook (via ``auth_devices``).

The prod incident (avelyra): Adapty sends our **deviceId** in ``customer_user_id`` (the id iOS
passed to ``Adapty.identify``), NOT our internal ``userId``. Stage 3 previously only checked
``users.id`` -> ``ignored/user_not_found`` -> the subscription silently lapsed. ADR-055 replaces the
one-step existence check with the shared ``resolve_user`` (ADR-055 §A): (a) X in ``users`` ->
``resolvedVia="user_id"``; (b) else X in ``auth_devices.device_id`` -> take the linked ``user_id``
(``resolvedVia="device_id"``); (c) else -> ``user_not_found``. Every accrual (dedup event row,
subscription upsert, wallet grant + ledger, audit) then keys on the RESOLVED userId — never on the
raw deviceId.

Drives ``AdaptyWebhookService.handle()`` DIRECTLY against the REAL testcontainers Postgres (seeded
via ``seed_user`` + a direct ``auth_devices`` insert), plus ONE full-HTTP test to pin the anti-retry
``ignored -> HTTP 200`` contract. Hermetic: no network, no real Adapty/LLM; ``Settings`` built
inline / injected via env. In-process Alembic disables the service logger under ``caplog`` -> the
fixture re-enables it (a test-harness artifact only; see ADR-046 suite).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.billing_adapty.service import AdaptyWebhookService
from app.config import Settings, get_settings
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "adapty_webhook_outcome"
_LOGGER = "app.billing_adapty.service"
_SECRET = "adapty-webhook-secret-value"  # noqa: S105 - test-only static secret
_WEEK_PRODUCT = "week_6.99_nottrial"
_WEEK_TOKENS = 700  # mapped tier (distinct from the fallback)
_FALLBACK = 1000
_PRODUCT_TOKENS = json.dumps({_WEEK_PRODUCT: _WEEK_TOKENS})

# U is our internal userId (a ``users`` row); D is a deviceId (``auth_devices.device_id`` -> U) that
# is NOT itself a ``users`` row — the exact prod incident shape (D = 35a95d9b-... in the ADR).
_U = uuid.UUID("894edaee-0902-4cf6-82d4-c4ca13787cb4")
_D = uuid.UUID("35a95d9b-86bf-4d69-a5c8-8790e25fd9af")


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
    """Service bound to the container DB session (real Wallet/Audit, inline Settings)."""
    audit = AuditService(db_session)
    yield AdaptyWebhookService(db_session, WalletService(db_session, audit), audit, _settings())


def _event(
    *,
    profile_event_id: str,
    event_type: str,
    customer_user_id: uuid.UUID | str | None,
    transaction_id: str | int | None = 410003298316682,
    vendor_product_id: str | None = _WEEK_PRODUCT,
    is_active: Any = None,
    access_level_id: str | None = None,
) -> bytes:
    """Build one Adapty event in the real wire shape (business fields under event_properties)."""
    ep: dict[str, Any] = {}
    if transaction_id is not None:
        ep["transaction_id"] = transaction_id
    if vendor_product_id is not None:
        ep["vendor_product_id"] = vendor_product_id
    if is_active is not None:
        ep["is_active"] = is_active
    if access_level_id is not None:
        ep["access_level_id"] = access_level_id
    body: dict[str, Any] = {
        "profile_event_id": profile_event_id,
        "event_type": event_type,
        "event_properties": ep,
    }
    if customer_user_id is not None:
        body["customer_user_id"] = str(customer_user_id)
    return json.dumps(body).encode()


# --------------------------- DB helpers (fresh connection, post-commit) ---------------------------


async def _seed_device(
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


async def _audit_rows(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> list[dict[str, Any]]:
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
    return [dict(r.payload) for r in rows]


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


# ============================ (a) direct userId — backward compat ============================


@pytest.mark.asyncio
async def test_direct_userid_resolves_via_user_id(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # customer_user_id IS our userId (present in users) -> path (a): resolvedVia="user_id",
    # accrual on U. Behaviour strictly unchanged from before ADR-055.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    caplog.set_level(logging.DEBUG)

    raw = _event(profile_event_id="evt-a", event_type="subscription_started", customer_user_id=_U)
    outcome = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert outcome.result == "applied"
    assert await _subscription(db_sessionmaker, _U) == ("active", _WEEK_PRODUCT)
    assert await _balance(db_sessionmaker, _U) == _WEEK_TOKENS

    fields = _rendered(_outcomes(caplog)[0])
    assert fields["resolvedVia"] == "user_id"
    assert fields["resolvedUserId"] == str(_U)
    assert fields["customerUserId"] == str(_U)


# ==================== (b) deviceId -> linked user (the prod incident) ====================


@pytest.mark.asyncio
async def test_device_id_resolves_and_credits_linked_user(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # THE incident: customer_user_id = D (a deviceId in auth_devices -> U, NOT a users row). Every
    # accrual must land on the RESOLVED U, never on D. The log carries customerUserId=D (original)
    # and resolvedUserId=U (real recipient), resolvedVia="device_id".
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)
    caplog.set_level(logging.DEBUG)

    raw = _event(
        profile_event_id="evt-incident", event_type="subscription_renewed", customer_user_id=_D
    )
    outcome = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert outcome.result == "applied"

    # ALL records target the resolved U.
    assert await _event_user_ids(db_sessionmaker, "evt-incident") == [str(_U)]
    assert await _subscription(db_sessionmaker, _U) == ("active", _WEEK_PRODUCT)
    assert await _balance(db_sessionmaker, _U) == _WEEK_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == ["adapty-txn:410003298316682"]
    audits = await _audit_rows(db_sessionmaker, _U)
    assert len(audits) == 1
    # The audit payload preserves the ORIGINAL Adapty identifier (D) for tracing.
    assert audits[0]["customerId"] == str(_D)

    # D never appears as a user_id anywhere.
    assert await _subscription(db_sessionmaker, _D) is None
    assert await _balance(db_sessionmaker, _D) is None
    assert await _ledger_keys(db_sessionmaker, _D) == []
    assert await _audit_rows(db_sessionmaker, _D) == []

    fields = _rendered(_outcomes(caplog)[0])
    assert fields["resolvedVia"] == "device_id"
    assert fields["resolvedUserId"] == str(_U)  # the real recipient
    assert fields["customerUserId"] == str(_D)  # the original Adapty id (a deviceId)


@pytest.mark.asyncio
async def test_device_resolved_grant_idempotent_same_txn(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Two DISTINCT granting events (distinct profile_event_id) sharing ONE transaction_id, both
    # device-resolved -> exactly ONE grant on U (adapty-txn idempotency unchanged by ADR-055).
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)

    first = _event(profile_event_id="evt-g1", event_type="trial_started", customer_user_id=_D)
    assert (await adapty_service.handle(first)).result == "applied"
    await adapty_service._session.commit()

    second = _event(
        profile_event_id="evt-g2",
        event_type="access_level_updated",
        customer_user_id=_D,
        is_active=True,
        access_level_id="premium",
    )
    assert (await adapty_service.handle(second)).result == "applied"
    await adapty_service._session.commit()

    # One grant despite two granting-events (same txn); balance == a single tier.
    assert await _ledger_keys(db_sessionmaker, _U) == ["adapty-txn:410003298316682"]
    assert await _balance(db_sessionmaker, _U) == _WEEK_TOKENS


@pytest.mark.asyncio
async def test_device_resolved_duplicate_event_id_no_mutation_keeps_resolve_fields(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Replay of the SAME profile_event_id (device-resolved) -> duplicate: no second grant, event
    # dedup unchanged, and the duplicate outcome STILL carries resolvedVia/resolvedUserId.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)
    raw = _event(profile_event_id="evt-dup", event_type="subscription_started", customer_user_id=_D)

    assert (await adapty_service.handle(raw)).result == "applied"
    await adapty_service._session.commit()

    caplog.set_level(logging.DEBUG)
    second = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert second.result == "duplicate"
    assert await _balance(db_sessionmaker, _U) == _WEEK_TOKENS  # unchanged on replay
    assert await _ledger_keys(db_sessionmaker, _U) == ["adapty-txn:410003298316682"]
    fields = _rendered(_outcomes(caplog)[0])
    assert fields["result"] == "duplicate"
    assert fields["resolvedVia"] == "device_id"
    assert fields["resolvedUserId"] == str(_U)


@pytest.mark.asyncio
async def test_unknown_event_type_device_resolved_keeps_resolve_fields(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A device-resolved but UNKNOWN event_type -> ignored-echo (reason absent) but resolvedVia /
    # resolvedUserId ARE present (resolution happened before the event-type dispatch). No mutation.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)
    caplog.set_level(logging.DEBUG)

    raw = _event(
        profile_event_id="evt-unknown", event_type="subscription_paused", customer_user_id=_D
    )
    outcome = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert outcome.result == "ignored"
    assert outcome.reason is None  # the unknown-type echo
    # No mutation for an unknown type.
    assert await _balance(db_sessionmaker, _U) is None
    assert await _event_user_ids(db_sessionmaker, "evt-unknown") == []

    fields = _rendered(_outcomes(caplog)[0])
    assert "reason" not in fields  # None -> dropped by the formatter
    assert fields["eventType"] == "subscription_paused"  # echoed
    assert fields["resolvedVia"] == "device_id"
    assert fields["resolvedUserId"] == str(_U)
    assert fields["customerUserId"] == str(_D)


# ==================== (c) unresolvable -> user_not_found (no resolve fields) ====================


@pytest.mark.asyncio
async def test_unresolvable_identifier_is_user_not_found_no_resolve_fields(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A well-formed UUID in NEITHER users NOR auth_devices -> user_not_found (WARNING). No mutation;
    # resolvedVia / resolvedUserId are ABSENT (resolution never succeeded); customerUserId present.
    stranger = uuid.uuid4()
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)  # unrelated user
    caplog.set_level(logging.DEBUG)

    raw = _event(
        profile_event_id="evt-nouser", event_type="subscription_renewed", customer_user_id=stranger
    )
    outcome = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert (outcome.result, outcome.reason) == ("ignored", "user_not_found")
    assert await _balance(db_sessionmaker, stranger) is None
    assert await _balance(db_sessionmaker, _U) is None

    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    assert fields["reason"] == "user_not_found"
    assert fields["customerUserId"] == str(stranger)
    assert "resolvedVia" not in fields
    assert "resolvedUserId" not in fields


# ==================== first-match: users wins over device (integration) ====================


@pytest.mark.asyncio
async def test_first_match_users_over_device(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # X is BOTH a users row AND an auth_devices.device_id linked to a DIFFERENT user -> users wins:
    # resolvedVia="user_id", accrual on X; the device-linked user is NOT credited.
    other = uuid.uuid4()
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
        await seed_user(s, user_id=other)
    await _seed_device(db_sessionmaker, device_id=str(_U), user_id=other)
    caplog.set_level(logging.DEBUG)

    raw = _event(
        profile_event_id="evt-both", event_type="subscription_started", customer_user_id=_U
    )
    outcome = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _WEEK_TOKENS
    assert await _balance(db_sessionmaker, other) is None  # the device-linked user is untouched
    assert _rendered(_outcomes(caplog)[0])["resolvedVia"] == "user_id"


# ==================== regression: missing_customer_user_id semantics unchanged ====================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "customer_user_id",
    [None, "not-a-uuid", "35a95d9b-86bf-4d69-a5c8"],  # absent / non-UUID / truncated
)
async def test_missing_or_nonuuid_customer_user_id_unchanged(
    adapty_service: AdaptyWebhookService,
    caplog: pytest.LogCaptureFixture,
    customer_user_id: str | None,
) -> None:
    # Regression (ADR-055 §D): an absent or non-UUID customer_user_id still ->
    # missing_customer_user_id (WARNING); resolution is never reached, resolvedVia/resolvedUserId
    # absent. Semantics unchanged.
    caplog.set_level(logging.DEBUG)
    raw = _event(
        profile_event_id="evt-missing-cuid",
        event_type="subscription_started",
        customer_user_id=customer_user_id,
    )
    outcome = await adapty_service.handle(raw)

    assert (outcome.result, outcome.reason) == ("ignored", "missing_customer_user_id")
    fields = _rendered(_outcomes(caplog)[0])
    assert fields["reason"] == "missing_customer_user_id"
    assert fields["eventType"] == "subscription_started"
    assert "resolvedVia" not in fields
    assert "resolvedUserId" not in fields
    assert "customerUserId" not in fields


# ==================== HTTP contract: ignored/user_not_found -> 200 (anti-retry) ============


@pytest.fixture
async def adapty_http_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """Full ASGI client wired to the container DB with ADAPTY_* env (secret + tier map)."""
    from app import deps
    from app.api_gateway import rate_limit
    from app.main import create_app

    monkeypatch.setenv("ADAPTY_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("ADAPTY_PRODUCT_TOKENS", _PRODUCT_TOKENS)
    monkeypatch.setenv("ADAPTY_SUBSCRIPTION_TOKENS_GRANT", str(_FALLBACK))
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


@pytest.mark.asyncio
async def test_user_not_found_returns_http_200(adapty_http_client: AsyncClient) -> None:
    # Anti-retry contract (ADR-055 §D): an unresolvable customer_user_id must map to HTTP 200 with
    # ignored/user_not_found so Adapty does NOT retry the drop forever.
    stranger = uuid.uuid4()
    raw = _event(
        profile_event_id="evt-http-nouser",
        event_type="subscription_renewed",
        customer_user_id=stranger,
    )
    r = await adapty_http_client.post(
        "/v1/billing/adapty/webhook",
        content=raw,
        headers={"Authorization": f"Bearer {_SECRET}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "user_not_found"}


@pytest.mark.asyncio
async def test_device_resolved_returns_http_200_applied(
    adapty_http_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Full HTTP path for the incident shape: device-resolved renewal -> 200 applied, credited on U.
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=_U)
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)

    raw = _event(
        profile_event_id="evt-http-device", event_type="subscription_renewed", customer_user_id=_D
    )
    r = await adapty_http_client.post(
        "/v1/billing/adapty/webhook",
        content=raw,
        headers={"Authorization": f"Bearer {_SECRET}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "applied"}
    assert await _balance(db_sessionmaker, _U) == _WEEK_TOKENS
    assert await _balance(db_sessionmaker, _D) is None
