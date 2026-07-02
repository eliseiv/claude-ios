"""CloudPaymentsWebhookService: defensive parse -> classify -> dedup -> apply -> audit (ADR-050).

Implements ADR-050 §2-§7 / billing-cloudpayments/03-architecture.md. ``handle(raw)`` never raises on
a malformed / unrecognised callback — it returns an ``ignored`` / ``duplicate`` / ``applied``
outcome (the router maps every one of these to HTTP 200 ``{"code": 0}``). It DOES raise on a real
internal failure (e.g. the DB is unavailable): the caller's session_scope then rolls the whole
transaction back and the router surfaces 500, which the aggregator retries — on retry the
``transaction_id`` is free again (the INSERT was rolled back) so reprocessing is clean (and
``grant`` is additionally idempotent by ``cp-txn:{transaction_id}``).

Two independent idempotency layers (ADR-050 §4): event-delivery dedup lives in the single statement
``INSERT cloudpayments_webhook_events ... ON CONFLICT (transaction_id) DO NOTHING RETURNING``; empty
RETURNING => duplicate => no mutations. Credit-grant idempotency is separate — keyed by
``cp-txn:{transaction_id}`` (namespace isolated from Adapty / StoreKit). Both, plus the subscription
upsert / token grant and the audit row, run in the SAME transaction.

Anti-tamper (ADR-050 §3, ADR-015 BR-TP-1): credit amounts come ONLY from server-side maps
(``cloudpayments_product_tokens`` + fallback, or ``token_products``) — NEVER from the payload
``Amount``/``recurring_amount``. Card data (``CardFirstSix``/``CardLastFour``/``Issuer`` /
``CardType``) and the bearer are never read into business logic, logged, or persisted.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_CLOUDPAYMENTS_PAYMENT, AuditEvent, AuditService
from app.billing_cloudpayments import parser
from app.billing_cloudpayments.parser import ParsedPayment
from app.config import Settings
from app.observability.logging import log_event
from app.wallet.service import WalletService

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.service"


@dataclass(frozen=True)
class WebhookOutcome:
    """Result of handling one webhook call. The router maps it to an HTTP-200 ``{"code": 0}``."""

    result: str  # "ignored" | "duplicate" | "applied"
    reason: str | None = None


def _ignored(reason: str) -> WebhookOutcome:
    return WebhookOutcome(result="ignored", reason=reason)


def _level_for(result: str, reason: str | None) -> int:
    """Map a webhook outcome to a log level (ADR-050 §7 / 08-observability.md).

    ``user_not_found`` / ``unknown_product`` are WARNING (a payment arrived but the credit could not
    be delivered — the incident class). ``applied`` / ``duplicate`` and the parse-shape reasons are
    INFO; a fully empty body is a low-signal connectivity probe -> DEBUG.
    """
    if result in ("applied", "duplicate"):
        return logging.INFO
    # result == "ignored"
    if reason in ("user_not_found", "unknown_product"):
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    # invalid_json | not_an_object | not_a_completed_payment | missing_transaction_id
    # | invalid_data | missing_product_id | invalid_account_id
    return logging.INFO


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class CloudPaymentsWebhookService:
    def __init__(
        self,
        session: AsyncSession,
        wallet: WalletService,
        audit: AuditService,
        settings: Settings,
    ) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit
        self._settings = settings

    def _log_outcome(
        self,
        outcome: WebhookOutcome,
        *,
        transaction_id: str | None = None,
        product_id: str | None = None,
        user_id: uuid.UUID | None = None,
        kind: str | None = None,
    ) -> WebhookOutcome:
        """Emit exactly one structured ``cloudpayments_webhook_outcome`` record; return ``outcome``.

        Only the fixed allowlist is logged (result / reason / transactionId / productId / userId /
        kind). ``user_id`` is our internal UUID -> ``str(...)`` so ``json.dumps`` in the
        ``JsonFormatter`` cannot fail; ``None`` fields are dropped from the JSON (= "not parsed").
        Card data, the bearer and the raw payload are never logged (ADR-050 §7).
        """
        level = _level_for(outcome.result, outcome.reason)
        log_event(
            logger,
            level,
            "cloudpayments_webhook_outcome",
            result=outcome.result,
            reason=outcome.reason,
            transactionId=transaction_id,
            productId=product_id,
            userId=str(user_id) if user_id is not None else None,
            kind=kind,
        )
        return outcome

    async def handle(self, raw: bytes) -> WebhookOutcome:
        """Process one raw callback body. Always returns a 200-mappable outcome unless the DB fails.

        Pre-transaction validation (empty / not-JSON / not-object / gate / missing fields /
        invalid account / user-not-found / unknown product) yields ``ignored`` with no DB writes.
        A valid, known, grantable payment is applied inside the caller's transaction; any real DB
        failure propagates (=> rollback/500).
        """
        # --- Stage 1: body shape (no DB) ---
        if not raw:
            return self._log_outcome(_ignored("empty_body"))
        try:
            body = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._log_outcome(_ignored("invalid_json"))
        if not isinstance(body, dict):
            return self._log_outcome(_ignored("not_an_object"))

        # --- Stage 2: defensive field parsing (no DB) ---
        transaction_id = parser.parse_transaction_id(body)
        if transaction_id is None:
            return self._log_outcome(_ignored("missing_transaction_id"))

        status = parser.parse_status(body)
        operation_type = parser.parse_operation_type(body)
        if not parser.parse_gate(status, operation_type):
            return self._log_outcome(
                _ignored("not_a_completed_payment"), transaction_id=transaction_id
            )

        data = parser._parse_data(body)
        if data is None:
            return self._log_outcome(_ignored("invalid_data"), transaction_id=transaction_id)

        product_id = parser.parse_product_id(data)
        if product_id is None:
            return self._log_outcome(_ignored("missing_product_id"), transaction_id=transaction_id)

        user_id = parser.parse_user_id(body, data)
        if user_id is None:
            return self._log_outcome(
                _ignored("invalid_account_id"),
                transaction_id=transaction_id,
                product_id=product_id,
            )

        # --- Stage 3: user existence (DB read; we never provision users here) ---
        if not await self._user_exists(user_id):
            return self._log_outcome(
                _ignored("user_not_found"),
                transaction_id=transaction_id,
                product_id=product_id,
                user_id=user_id,
            )

        # --- Stage 4: single classification point (ADR-050 §3; findings: classify ONCE here) ---
        billing_interval_unit = parser.parse_billing_interval_unit(data)
        token_products = self._settings.token_products()
        kind = parser.classify_product(product_id, billing_interval_unit, frozenset(token_products))
        if kind == parser.KIND_UNKNOWN:
            return self._log_outcome(
                _ignored("unknown_product"),
                transaction_id=transaction_id,
                product_id=product_id,
                user_id=user_id,
                kind=kind,
            )
        # Token package whose id matched by name/map but has no positive credit entry -> anti-tamper
        # (never size a grant from the payload / product name). Checked BEFORE the dedup INSERT.
        if kind == parser.KIND_TOKENS and token_products.get(product_id, 0) <= 0:
            return self._log_outcome(
                _ignored("unknown_product"),
                transaction_id=transaction_id,
                product_id=product_id,
                user_id=user_id,
                kind=kind,
            )

        is_trial_initial, is_trial_conversion, is_initial_payment = parser.parse_trial_flags(data)
        parsed = ParsedPayment(
            transaction_id=transaction_id,
            user_id=user_id,
            product_id=product_id,
            status=status,
            operation_type=operation_type,
            billing_interval_unit=billing_interval_unit,
            billing_interval_count=parser.parse_billing_interval_count(data),
            billing_phase=parser.parse_billing_phase(data),
            subscription_id=parser.parse_subscription_id(data, body),
            is_trial_initial=is_trial_initial,
            is_trial_conversion=is_trial_conversion,
            is_initial_payment=is_initial_payment,
            amount=parser.parse_amount(body),
            currency=parser.parse_currency(body),
            test_mode=parser.parse_test_mode(body),
            kind=kind,
        )
        return await self._apply(parsed, token_products)

    async def _user_exists(self, user_id: uuid.UUID) -> bool:
        exists = await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :uid"),
            {"uid": str(user_id)},
        )
        return exists is not None

    async def _apply(self, parsed: ParsedPayment, token_products: dict[str, int]) -> WebhookOutcome:
        """Apply a valid, known, grantable payment inside the caller's single transaction (ADR-050).

        The INSERT ... ON CONFLICT DO NOTHING RETURNING is the sole event-delivery dedup point: an
        empty RETURNING means a previous/concurrent delivery already recorded this transaction_id ->
        duplicate, no mutations. Otherwise, by ``kind``: subscription upserts an active subscription
        and grants per-tier credits; tokens grants a one-time package (subscription untouched). The
        grant is idempotent by ``cp-txn:{transaction_id}``. The audit row runs in this transaction.
        """
        sanitized = parser.sanitize_payload(parsed)
        inserted = await self._session.scalar(
            text(
                "INSERT INTO cloudpayments_webhook_events "
                "(transaction_id, user_id, product_id, kind, payload) "
                "VALUES (:txn, :uid, :product_id, :kind, CAST(:payload AS JSONB)) "
                "ON CONFLICT (transaction_id) DO NOTHING "
                "RETURNING transaction_id"
            ),
            {
                "txn": parsed.transaction_id,
                "uid": str(parsed.user_id),
                "product_id": parsed.product_id,
                "kind": parsed.kind,
                "payload": json.dumps(sanitized),
            },
        )
        if inserted is None:
            # Duplicate transaction_id: no mutations (ADR-050 §4).
            return self._log_outcome(
                WebhookOutcome(result="duplicate"),
                transaction_id=parsed.transaction_id,
                product_id=parsed.product_id,
                user_id=parsed.user_id,
                kind=parsed.kind,
            )

        sub_status: str | None = None
        plan: str | None = None
        expires_at: datetime.datetime | None = None
        if parsed.kind == parser.KIND_SUBSCRIPTION:
            expires_at = parser._compute_expiry(
                _now(), parsed.billing_interval_unit, parsed.billing_interval_count
            )
            plan = parsed.product_id
            sub_status = "active"
            await self._upsert_subscription(parsed.user_id, plan, expires_at)
            credits = (
                self._settings.cloudpayments_product_tokens().get(parsed.product_id)
                or self._settings.cloudpayments_subscription_tokens_grant
            )
            await self._grant(parsed, credits, reason="cloudpayments_subscription")
        else:
            # tokens: token_products[product_id] is guaranteed > 0 (checked in handle()).
            credits = token_products[parsed.product_id]
            await self._grant(parsed, credits, reason="cloudpayments_tokens")

        await self._audit.record(
            AuditEvent(
                user_id=parsed.user_id,
                event_type=EVENT_CLOUDPAYMENTS_PAYMENT,
                payload={
                    "transactionId": parsed.transaction_id,
                    "productId": parsed.product_id,
                    "kind": parsed.kind,
                    "semantics": parsed.kind,
                    "status": sub_status,
                    "plan": plan,
                    "expiresAt": expires_at.isoformat() if expires_at is not None else None,
                    "creditsGranted": credits,
                    "billingPhase": parsed.billing_phase,
                    "amount": parsed.amount,
                    "currency": parsed.currency,
                    "testMode": parsed.test_mode,
                    "subscriptionId": parsed.subscription_id,
                },
            )
        )
        return self._log_outcome(
            WebhookOutcome(result="applied"),
            transaction_id=parsed.transaction_id,
            product_id=parsed.product_id,
            user_id=parsed.user_id,
            kind=parsed.kind,
        )

    async def _upsert_subscription(
        self, user_id: uuid.UUID, plan: str, expires_at: datetime.datetime
    ) -> None:
        """Activate/extend the subscription in one statement (ADR-048 pattern, ADR-050 §3a).

        ON CONFLICT (user_id) makes a concurrent FIRST activation idempotent by PK. Parameterized;
        ``'active'`` casts to the subscription-status enum.
        """
        await self._session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at) "
                "VALUES (:uid, 'active', :plan, :expires_at, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "status = 'active', plan = EXCLUDED.plan, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()"
            ),
            {"uid": str(user_id), "plan": plan, "expires_at": expires_at},
        )
        await self._session.flush()

    async def _grant(self, parsed: ParsedPayment, credits: int, *, reason: str) -> None:
        """Grant credits idempotently by ``cp-txn:{transaction_id}`` (ADR-050 §4).

        One payment (unique TransactionId) grants exactly once; a renewal (new TransactionId) grants
        afresh. The amount is passed in from a server-side map only (anti-tamper).
        """
        await self._wallet.grant(
            user_id=parsed.user_id,
            amount=credits,
            idempotency_key=f"cp-txn:{parsed.transaction_id}",
            reason=reason,
            meta={
                "transactionId": parsed.transaction_id,
                "productId": parsed.product_id,
                "kind": parsed.kind,
            },
        )
