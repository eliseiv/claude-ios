"""Integration: ADR-050 — CloudPayments webhook outcome reasons, PII-safe logging, migration 0014.

The HTTP route collapses every processed outcome to ``{"code": 0}``, so the precise
``ignored/<reason>`` classification is asserted by driving ``CloudPaymentsWebhookService.handle()``
DIRECTLY against the REAL PostgreSQL container (the outcome logging lives in the service, not the
router). Hermetic: DB is the shared testcontainers Postgres (seeded via ``seed_user``); ``Settings``
is built inline; no network to broadapps/YooKassa; no LLM.

Covers §2 gate/shape reasons, §5 user_not_found (WARNING), §3 unknown_product (WARNING), §7 the
structured outcome log carries NO card data / bearer, the ``_level_for`` table, and migration 0014
(single alembic head + ``transaction_id`` PRIMARY KEY / UNIQUE).
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
from app.billing_cloudpayments.service import CloudPaymentsWebhookService, _level_for
from app.config import Settings
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "cloudpayments_webhook_outcome"
_SUB_PRODUCT = "yearly_49.99_nottrial"
_TOKEN_PRODUCT = "100_tokens_9.99"
_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"
_TXN = "31d884c8-000f-5001-8000-1fb75b44e1d9"
_SENTINEL = "Bearer super-secret-cloudpayments-token-DO-NOT-LOG"  # noqa: S105 - test sentinel


def _settings() -> Settings:
    return Settings(
        CLOUDPAYMENTS_WEBHOOK_TOKEN="x",  # noqa: S106 - test-only static secret
        CLOUDPAYMENTS_PRODUCT_TOKENS=json.dumps({_SUB_PRODUCT: 12000}),
        CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT=1000,
        TOKEN_PRODUCTS=json.dumps({_TOKEN_PRODUCT: 100}),
    )


@pytest.fixture
async def cp_service(db_session: AsyncSession) -> AsyncIterator[CloudPaymentsWebhookService]:
    """Service bound to the container DB session (real Wallet/Audit, inline Settings).

    Re-enable the service logger: the in-process Alembic migration (fileConfig default
    ``disable_existing_loggers=True``) disables loggers created before it ran — including
    ``app.billing_cloudpayments.service`` — which would swallow records under ``caplog``.
    Test-harness artifact only (production runs migrations in a separate process).
    """
    logging.getLogger("app.billing_cloudpayments.service").disabled = False
    audit = AuditService(db_session)
    yield CloudPaymentsWebhookService(
        db_session, WalletService(db_session, audit), audit, _settings()
    )


def _body(
    *,
    data: dict[str, Any] | str | None,
    transaction_id: str | None = _TXN,
    account_id: str | None = _UID_UPPER,
    status: str = "Completed",
    operation_type: str = "Payment",
    with_card: bool = False,
) -> bytes:
    body: dict[str, Any] = {"Status": status, "OperationType": operation_type, "Currency": "RUB"}
    if data is not None:
        body["Data"] = json.dumps(data) if isinstance(data, dict) else data
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


def _sub_data(**extra: Any) -> dict[str, Any]:
    d = {"user_id": _UID_UPPER, "product_id": _SUB_PRODUCT, "billing_interval_unit": "year"}
    d.update(extra)
    return d


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


# ============================ Outcome reasons (one record each) ============================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, reason, level",
    [
        (b"", "empty_body", logging.DEBUG),
        (b"not json <<<", "invalid_json", logging.INFO),
        (b"[1,2,3]", "not_an_object", logging.INFO),
    ],
)
async def test_body_shape_reasons(
    cp_service: CloudPaymentsWebhookService,
    caplog: pytest.LogCaptureFixture,
    raw: bytes,
    reason: str,
    level: int,
) -> None:
    caplog.set_level(logging.DEBUG)
    outcome = await cp_service.handle(raw)
    assert (outcome.result, outcome.reason) == ("ignored", reason)
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == level


@pytest.mark.asyncio
async def test_missing_transaction_id_reason(cp_service: CloudPaymentsWebhookService) -> None:
    outcome = await cp_service.handle(_body(data=_sub_data(), transaction_id=None))
    assert (outcome.result, outcome.reason) == ("ignored", "missing_transaction_id")


@pytest.mark.asyncio
async def test_gate_not_a_completed_payment_reason(
    cp_service: CloudPaymentsWebhookService,
) -> None:
    outcome = await cp_service.handle(_body(data=_sub_data(), status="Pending"))
    assert (outcome.result, outcome.reason) == ("ignored", "not_a_completed_payment")


@pytest.mark.asyncio
async def test_invalid_data_reason(cp_service: CloudPaymentsWebhookService) -> None:
    outcome = await cp_service.handle(_body(data="{not json"))
    assert (outcome.result, outcome.reason) == ("ignored", "invalid_data")


@pytest.mark.asyncio
async def test_missing_product_id_reason(cp_service: CloudPaymentsWebhookService) -> None:
    outcome = await cp_service.handle(_body(data={"user_id": _UID_UPPER}))
    assert (outcome.result, outcome.reason) == ("ignored", "missing_product_id")


@pytest.mark.asyncio
async def test_invalid_account_id_reason(cp_service: CloudPaymentsWebhookService) -> None:
    data = {"product_id": _SUB_PRODUCT, "billing_interval_unit": "year"}
    outcome = await cp_service.handle(_body(data=data, account_id=None))
    assert (outcome.result, outcome.reason) == ("ignored", "invalid_account_id")


@pytest.mark.asyncio
async def test_user_not_found_reason_and_warning(
    cp_service: CloudPaymentsWebhookService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    uid = uuid.uuid4()  # valid UUID, no users row
    outcome = await cp_service.handle(
        _body(
            data={"product_id": _SUB_PRODUCT, "billing_interval_unit": "year"},
            account_id=str(uid).upper(),
        )
    )
    assert (outcome.result, outcome.reason) == ("ignored", "user_not_found")
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    # ADR-053: on user_not_found X is only a candidate (not in users nor auth_devices), so it is
    # NOT a resolved userId -> userId (and resolvedVia) are omitted from the outcome log.
    assert "userId" not in fields
    assert "resolvedVia" not in fields


@pytest.mark.asyncio
async def test_unknown_product_reason_and_warning(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, user_id=uuid.UUID(_UID_UPPER.lower()))
    caplog.set_level(logging.DEBUG)
    # A product with no interval / not in any map / not a sub-name -> unknown_product (WARNING).
    outcome = await cp_service.handle(
        _body(data={"user_id": _UID_UPPER, "product_id": "randomsku"})
    )
    assert (outcome.result, outcome.reason) == ("ignored", "unknown_product")
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    assert _rendered(recs[0])["userId"] == str(uid)


@pytest.mark.asyncio
async def test_token_name_absent_from_map_is_unknown_product(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Anti-tamper: token-name pattern but not in TOKEN_PRODUCTS -> unknown_product (never guess N).
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uuid.UUID(_UID_UPPER.lower()))
    outcome = await cp_service.handle(
        _body(data={"user_id": _UID_UPPER, "product_id": "999_tokens_pack"})
    )
    assert (outcome.result, outcome.reason) == ("ignored", "unknown_product")


# ============================ Applied / duplicate ============================


@pytest.mark.asyncio
async def test_applied_then_duplicate(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uuid.UUID(_UID_UPPER.lower()))
    caplog.set_level(logging.DEBUG)

    first = await cp_service.handle(_body(data=_sub_data()))
    await cp_service._session.commit()
    assert first.result == "applied"
    assert len(_outcomes(caplog)) == 1
    assert _outcomes(caplog)[0].levelno == logging.INFO

    caplog.clear()
    second = await cp_service.handle(_body(data=_sub_data()))
    await cp_service._session.commit()
    assert second.result == "duplicate"
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO


# ============================ §7 PII-safe log (no card data / bearer) ============================


@pytest.mark.asyncio
async def test_outcome_log_carries_no_card_data_or_secret(
    cp_service: CloudPaymentsWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uuid.UUID(_UID_UPPER.lower()))
    caplog.set_level(logging.DEBUG)
    # Plant card data + a bearer-like sentinel in the body; the fixed allowlist must drop them all.
    data = _sub_data(authorization=_SENTINEL)
    await cp_service.handle(_body(data=data, with_card=True))
    await cp_service._session.commit()

    recs = _outcomes(caplog)
    assert len(recs) == 1
    rendered = json.dumps(_rendered(recs[0]))
    for forbidden in ("CardFirstSix", "CardLastFour", "220024", "8808", "VTB", _SENTINEL):
        assert forbidden not in rendered
    assert "bearer" not in rendered.lower()
    # Only the fixed allowlist of outcome keys is present. This drives an APPLIED outcome for a
    # seeded user, so ADR-053's ``resolvedVia`` ("user_id") is now part of the allowlist.
    fields = _rendered(recs[0])
    assert set(fields) <= {
        "level",
        "logger",
        "message",
        "result",
        "reason",
        "transactionId",
        "productId",
        "userId",
        "kind",
        "resolvedVia",
        "requestId",
        "sessionId",
    }
    # ADR-053: the applied outcome for a directly-resolved userId carries resolvedVia="user_id".
    assert fields["resolvedVia"] == "user_id"


# ============================ _level_for table ============================


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("user_not_found", logging.WARNING),
        ("unknown_product", logging.WARNING),
        ("empty_body", logging.DEBUG),
        ("invalid_json", logging.INFO),
        ("not_a_completed_payment", logging.INFO),
        ("missing_transaction_id", logging.INFO),
        ("invalid_data", logging.INFO),
        ("missing_product_id", logging.INFO),
        ("invalid_account_id", logging.INFO),
    ],
)
def test_level_for_ignored(reason: str, expected: int) -> None:
    assert _level_for("ignored", reason) == expected


@pytest.mark.parametrize("result", ["applied", "duplicate"])
def test_level_for_applied_duplicate_info(result: str) -> None:
    assert _level_for(result, None) == logging.INFO


# ============================ Migration 0014 — single head + UNIQUE transaction_id ============


def test_alembic_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert heads == ["0014_cp_webhook_events"], heads


@pytest.mark.asyncio
async def test_migration_0014_table_and_unique_transaction_id(db_session: AsyncSession) -> None:
    # PRIMARY KEY on transaction_id (== UNIQUE) backs the ON CONFLICT dedup.
    pk_cols = (
        (
            await db_session.execute(
                text(
                    "SELECT kcu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON tc.constraint_name = kcu.constraint_name "
                    "WHERE tc.table_name = 'cloudpayments_webhook_events' "
                    "  AND tc.constraint_type = 'PRIMARY KEY'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert list(pk_cols) == ["transaction_id"]
