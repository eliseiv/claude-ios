"""AdaptyWebhookService: defensive parse -> dedup -> upsert subscription -> grant -> audit.

Implements ADR-029 §2-§7 / billing-adapty/03-architecture.md. ``handle(raw)`` never raises on a
malformed / unrecognised payload — it returns an ``ignored`` / ``duplicate`` / ``applied`` outcome
(the router maps every one of these to HTTP 200). It DOES raise on a real internal failure (e.g.
the DB is unavailable): the caller's session_scope then rolls the whole transaction back and the
router surfaces 500, which Adapty retries — on retry ``event_id`` is free again (the INSERT was
rolled back) so reprocessing is clean (and ``grant`` is additionally idempotent by key).

Two independent idempotency layers (ADR-047 §C): event-delivery dedup lives in the single statement
``INSERT adapty_webhook_events ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id`` (event_id
= ``profile_event_id``); empty RETURNING => duplicate => no mutations. Credit-grant idempotency is
separate — keyed by ``sub-grant:{transaction_id}`` (shared with StoreKit ``sync``, ADR-106 §A; the
historical ``adapty-txn:{transaction_id}`` is checked too) so one billing period grants exactly once
across the several granting-events Adapty emits per purchase AND across both channels. The
semantics-based dispatch (``classify_event`` -> GRANTING upserts+grants / EXPIRING expires / NOOP
keeps access / one-time purchase grants a token pack) + audit run in the SAME transaction as the
dedup INSERT.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_ADAPTY_SUBSCRIPTION, AuditEvent, AuditService
from app.billing_adapty import parser
from app.billing_adapty.parser import ParsedEvent
from app.billing_common.resolve import resolve_user
from app.billing_common.single_grant import grant_once, is_stale, signal_unmapped_product
from app.config import Settings
from app.instance_config import (
    CHANNEL_ADAPTY,
    SubscriptionCredits,
    is_product_unmapped,
    one_time_credits,
    subscription_credits,
)
from app.models import Subscription
from app.observability.logging import log_event
from app.wallet.service import WalletService

logger = logging.getLogger(__name__)  # == "app.billing_adapty.service"

# The grant key of a token pack is SHARED with POST /v1/tokens/purchase (ADR-106 §E2).
_TOKEN_PURCHASE_KEY_PREFIX = "token-purchase:"
_TOKEN_PURCHASE_SOURCE = "token_purchase"


@dataclass(frozen=True)
class WebhookOutcome:
    """Result of handling one webhook call. The router maps it to an HTTP-200 JSON envelope."""

    result: str  # "ignored" | "duplicate" | "applied"
    reason: str | None = None
    event_type: str | None = None


def _ignored(reason: str | None = None, event_type: str | None = None) -> WebhookOutcome:
    return WebhookOutcome(result="ignored", reason=reason, event_type=event_type)


def _level_for(result: str, reason: str | None) -> int:
    """Map a webhook outcome to a log level (ADR-046 §Таблица уровней / 08-observability.md).

    WARNING = "Adapty sent something event-like but the credit could not be delivered"
    (the incident class): ``user_not_found`` / ``missing_customer_user_id``, the token-pack
    refusals ``missing_transaction_id`` / ``unknown_product`` (ADR-106 §E4) and the unknown
    ``event_type`` echo (the only ``ignored`` outcome carrying ``reason is None``). ``applied`` /
    ``duplicate`` and the parse-shape garbage (``invalid_json`` / ``not_an_object`` /
    ``missing_event_id``) are INFO; a fully empty body is a low-signal connectivity probe -> DEBUG.
    """
    if result in ("applied", "duplicate"):
        return logging.INFO
    # result == "ignored"
    if reason in (
        "user_not_found",
        "missing_customer_user_id",
        "missing_transaction_id",
        "unknown_product",
    ):
        return logging.WARNING
    if reason is None:
        # The only ignored outcome with reason=None is the unknown-event_type echo.
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    # invalid_json | not_an_object | missing_event_id
    return logging.INFO


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class AdaptyWebhookService:
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
        event_type: str | None = None,
        event_id: str | None = None,
        customer_user_id: uuid.UUID | None = None,
        resolved_via: str | None = None,
        resolved_user_id: uuid.UUID | None = None,
        resolved_from: str | None = None,
        profile_id: uuid.UUID | None = None,
        product_id: str | None = None,
        transaction_id: str | None = None,
    ) -> WebhookOutcome:
        """Emit exactly one structured ``adapty_webhook_outcome`` record and return ``outcome``.

        Returns the same outcome so it can wrap an existing ``return`` without changing control
        flow (ADR-046 / 08-observability.md). Only the fixed allowlist is logged (result / reason /
        eventType / eventId / customerUserId / resolvedVia / resolvedUserId / resolvedFrom /
        profileId / productId / transactionId); the raw payload and the bearer secret are never
        logged. ``customer_user_id`` stays ONLY the ``customer_user_id`` from the body;
        ``profile_id`` is present when resolution went through it (ADR-106 §B3);
        ``product_id``/``transaction_id`` only on the token-pack branch (§E4). ``None`` fields are
        dropped from the JSON (= "not parsed" / "not resolved").
        """
        level = _level_for(outcome.result, outcome.reason)
        log_event(
            logger,
            level,
            "adapty_webhook_outcome",
            result=outcome.result,
            reason=outcome.reason,
            eventType=event_type,
            eventId=event_id,
            customerUserId=str(customer_user_id) if customer_user_id is not None else None,
            resolvedVia=resolved_via,
            resolvedUserId=str(resolved_user_id) if resolved_user_id is not None else None,
            resolvedFrom=resolved_from,
            profileId=str(profile_id) if profile_id is not None else None,
            productId=product_id,
            transactionId=transaction_id,
        )
        return outcome

    async def handle(self, raw: bytes) -> WebhookOutcome:
        """Process one raw webhook body. Always returns a 200-mappable outcome unless the DB fails.

        Pre-transaction validation (empty / not-JSON / not-object / missing id / missing user /
        user-not-found / unknown type / token-pack refusals) yields ``ignored`` with no DB writes.
        A recognised event is applied inside the caller's transaction; any real DB failure
        propagates (=> rollback/500).
        """
        # --- Stage 1: body shape (no DB) ---
        if not raw:
            return self._log_outcome(_ignored("empty_body"))
        try:
            body: Any = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._log_outcome(_ignored("invalid_json"))
        if not isinstance(body, dict):
            return self._log_outcome(_ignored("not_an_object"))

        # --- Stage 2: defensive field parsing (no DB) ---
        event_id = parser.parse_event_id(body)
        if event_id is None:
            return self._log_outcome(_ignored("missing_event_id"))
        # ADR-047 (synergy with ADR-046): parse event_type BEFORE the identifier check (a pure,
        # DB-free op) so the missing_customer_user_id WARNING carries the event type.
        event_type = parser.parse_event_type(body)
        customer_user_id = parser.parse_customer_user_id(body)
        profile_id = parser.parse_profile_id(body)
        if customer_user_id is None and profile_id is None:
            # ADR-106 §B2.3: no addressee in the body at all (reason name kept for dashboards).
            return self._log_outcome(
                _ignored("missing_customer_user_id"),
                event_type=event_type or None,
                event_id=event_id,
            )

        # --- Stage 3: user resolution (ADR-055 + ADR-106 §B2; DB read; never provision here) ---
        # customer_user_id first; profile_id when it is absent OR not resolved — same resolver.
        resolved: tuple[uuid.UUID, str] | None = None
        resolved_from = parser.RESOLVED_FROM_CUSTOMER_USER_ID
        tried_profile_id: uuid.UUID | None = None
        if customer_user_id is not None:
            resolved = await resolve_user(self._session, customer_user_id)
        if resolved is None and profile_id is not None:
            tried_profile_id = profile_id
            resolved_from = parser.RESOLVED_FROM_PROFILE_ID
            resolved = await resolve_user(self._session, profile_id)
        if resolved is None:
            # §B2.4: an identifier was present, none resolved -> user_not_found.
            return self._log_outcome(
                _ignored("user_not_found"),
                event_type=event_type,
                event_id=event_id,
                customer_user_id=customer_user_id,
                profile_id=tried_profile_id,
            )
        resolved_user_id, resolved_via = resolved

        # --- Stage 4: event-type dispatch ---
        if event_type not in parser.KNOWN_EVENTS:
            # Echo the (normalised) event_type so operators can see what arrived. No audit, no
            # mutation: an unknown type is "no event happened" (architect-reviewer minor).
            return self._log_outcome(
                _ignored(event_type=event_type),
                event_type=event_type,
                event_id=event_id,
                customer_user_id=customer_user_id,
                resolved_via=resolved_via,
                resolved_user_id=resolved_user_id,
                resolved_from=resolved_from,
                profile_id=tried_profile_id,
            )

        parsed = ParsedEvent(
            event_id=event_id,
            event_type=event_type,
            customer_user_id=customer_user_id,
            vendor_product_id=parser.parse_vendor_product_id(body),
            expires_at=parser.parse_expires_at(body),
            transaction_id=parser.parse_transaction_id(body),
            original_transaction_id=parser.parse_original_transaction_id(body),
            is_active=parser.parse_is_active(body),
            access_level_id=parser.parse_access_level_id(body),
            will_renew=parser.parse_will_renew(body),
            profile_id=tried_profile_id,
            resolved_from=resolved_from,
        )
        if parser.classify_event(parsed) == parser.SEM_ONE_TIME_PURCHASE:
            return await self._apply_purchase(parsed, body, resolved_user_id, resolved_via)
        return await self._apply(parsed, body, resolved_user_id, resolved_via)

    def _log_resolved(
        self,
        outcome: WebhookOutcome,
        event: ParsedEvent,
        resolved_user_id: uuid.UUID,
        resolved_via: str,
        *,
        purchase_fields: bool = False,
    ) -> WebhookOutcome:
        """Outcome log for an event whose user is resolved (shared field set)."""
        return self._log_outcome(
            outcome,
            event_type=event.event_type,
            event_id=event.event_id,
            customer_user_id=event.customer_user_id,
            resolved_via=resolved_via,
            resolved_user_id=resolved_user_id,
            resolved_from=event.resolved_from,
            profile_id=event.profile_id,
            product_id=event.vendor_product_id if purchase_fields else None,
            transaction_id=event.transaction_id if purchase_fields else None,
        )

    @staticmethod
    def _customer_id(event: ParsedEvent) -> str:
        """Audit ``customerId`` = the identifier the user was resolved by (ADR-106 §B3)."""
        if event.resolved_from == parser.RESOLVED_FROM_PROFILE_ID:
            return str(event.profile_id)
        return str(event.customer_user_id)

    async def _insert_event(
        self, event: ParsedEvent, body: dict[str, Any], resolved_user_id: uuid.UUID
    ) -> bool:
        """The sole event-delivery dedup point; ``False`` = this ``event_id`` was already recorded.

        Targets ``resolved_user_id`` — our internal userId (ADR-055), NOT the raw identifier.
        """
        inserted = await self._session.scalar(
            text(
                "INSERT INTO adapty_webhook_events (event_id, user_id, event_type, payload) "
                "VALUES (:event_id, :uid, :event_type, CAST(:payload AS JSONB)) "
                "ON CONFLICT (event_id) DO NOTHING "
                "RETURNING event_id"
            ),
            {
                "event_id": event.event_id,
                "uid": str(resolved_user_id),
                "event_type": event.event_type,
                "payload": json.dumps(body),
            },
        )
        return inserted is not None

    async def _apply_purchase(
        self,
        event: ParsedEvent,
        body: dict[str, Any],
        resolved_user_id: uuid.UUID,
        resolved_via: str,
    ) -> WebhookOutcome:
        """``non_subscription_purchase`` (ADR-106 §E): token pack under the app path's key.

        Refusals run BEFORE the dedup INSERT (no DB writes). The amount comes ONLY from the
        server-side one-time catalog (anti-tamper BR-TP-1); an active subscription is NOT required
        (§E3). ``subscriptions`` is neither read nor written.
        """
        if event.transaction_id is None:
            return self._log_resolved(
                _ignored("missing_transaction_id"),
                event,
                resolved_user_id,
                resolved_via,
                purchase_fields=True,
            )
        credits = (
            one_time_credits(event.vendor_product_id, settings=self._settings)
            if event.vendor_product_id
            else None
        )
        if credits is None:
            return self._log_resolved(
                _ignored("unknown_product"),
                event,
                resolved_user_id,
                resolved_via,
                purchase_fields=True,
            )

        if not await self._insert_event(event, body, resolved_user_id):
            return self._log_resolved(
                WebhookOutcome(result="duplicate"),
                event,
                resolved_user_id,
                resolved_via,
                purchase_fields=True,
            )

        key = f"{_TOKEN_PURCHASE_KEY_PREFIX}{event.transaction_id}"
        await grant_once(
            self._wallet,
            user_id=resolved_user_id,
            amount=credits,
            idempotency_key=key,
            check_keys=(key,),
            meta={
                "source": _TOKEN_PURCHASE_SOURCE,
                "productId": event.vendor_product_id,
                "transactionId": event.transaction_id,
                "eventType": event.event_type,
            },
            reason=_TOKEN_PURCHASE_SOURCE,
        )
        await self._audit.record(
            AuditEvent(
                user_id=resolved_user_id,
                event_type=EVENT_ADAPTY_SUBSCRIPTION,
                payload={
                    "adaptyEventId": event.event_id,
                    "eventType": event.event_type,
                    "semantics": parser.SEM_ONE_TIME_PURCHASE,
                    "productId": event.vendor_product_id,
                    "transactionId": event.transaction_id,
                    "resolvedFrom": event.resolved_from,
                    "customerId": self._customer_id(event),
                },
            )
        )
        return self._log_resolved(
            WebhookOutcome(result="applied"),
            event,
            resolved_user_id,
            resolved_via,
            purchase_fields=True,
        )

    async def _apply(
        self,
        event: ParsedEvent,
        body: dict[str, Any],
        resolved_user_id: uuid.UUID,
        resolved_via: str,
    ) -> WebhookOutcome:
        """Apply a recognised subscription event inside the caller's single transaction.

        An already-recorded ``event_id`` -> duplicate, no mutations. Otherwise the semantics are
        resolved by ``classify_event`` (GRANTING / EXPIRING / NOOP) and dispatched: GRANTING
        upserts an active subscription and grants credits (one grant per Apple period on any
        channel, ADR-106 §A); EXPIRING marks the subscription expired (credits untouched); NOOP
        (auto-renew turned off, access kept) touches neither subscription nor credits but is still
        recorded + audited. A stale event (ADR-106 §C) never rewrites the row. The audit row runs
        in this transaction, committed on success.
        """
        if not await self._insert_event(event, body, resolved_user_id):
            # Duplicate event_id: no mutations (ADR-029 §6, architect-reviewer minor).
            return self._log_resolved(
                WebhookOutcome(result="duplicate"), event, resolved_user_id, resolved_via
            )

        semantics = parser.classify_event(event)
        # transaction_id is unique per billing period (primary grant idem key);
        # original_transaction_id is stable across the chain (fallback); event_id is last resort.
        txn = event.transaction_id or event.original_transaction_id or event.event_id

        if semantics == parser.SEM_NOOP:
            # Auto-renew cancellation: access is kept until period end -> do NOT touch status /
            # expires_at / credits, but persist will_renew (false) so the client can show
            # "cancelled, ends at expiresAt". Echo the current subscription state into the audit.
            status, plan, stale = await self._read_subscription(
                resolved_user_id, event.will_renew, event.expires_at
            )
        else:
            status, plan, stale = await self._upsert_subscription(
                event, semantics, resolved_user_id
            )
            if semantics == parser.SEM_GRANTING:
                # Staleness does not decide the grant — the period key does (ADR-106 §C2).
                await self._grant(event, txn, resolved_user_id)

        await self._audit.record(
            AuditEvent(
                user_id=resolved_user_id,
                event_type=EVENT_ADAPTY_SUBSCRIPTION,
                payload={
                    "adaptyEventId": event.event_id,
                    "eventType": event.event_type,
                    "semantics": semantics,
                    "status": status,
                    "plan": plan,
                    "expiresAt": event.expires_at.isoformat() if event.expires_at else None,
                    "transactionId": txn,
                    "willRenew": event.will_renew,
                    "customerId": self._customer_id(event),
                    "resolvedFrom": event.resolved_from,
                    "stale": stale,
                },
            )
        )
        return self._log_resolved(
            WebhookOutcome(result="applied"), event, resolved_user_id, resolved_via
        )

    async def _read_subscription(
        self,
        user_id: uuid.UUID,
        will_renew: bool | None = None,
        expires_at: datetime.datetime | None = None,
    ) -> tuple[str | None, str | None, bool]:
        """Read the current (status, plan, stale) for the NOOP audit row.

        Does NOT change status/expires_at/credits, but persists ``will_renew`` on the existing row
        (auto-renew cancellation) so /policy/effective can surface it — unless the event is stale
        (ADR-106 §C3: a past period's cancellation does not concern the current one).
        """
        row = await self._session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        if row is None:
            return None, None, False
        stale = is_stale(row, expires_at)
        if not stale and will_renew is not None and row.will_renew != will_renew:
            row.will_renew = will_renew
            row.updated_at = _now()
        return row.status, row.plan, stale

    async def _upsert_subscription(
        self, event: ParsedEvent, semantics: str, resolved_user_id: uuid.UUID
    ) -> tuple[str, str | None, bool]:
        """Upsert subscriptions per the resolved semantics (ADR-047 §B) -> (status, plan, stale).

        Keyed by ``resolved_user_id`` — our internal userId (ADR-055). GRANTING -> active,
        plan=vendor_product_id, expires_at (if present). EXPIRING -> expired; plan and expires_at
        are left unchanged. NOOP never reaches here. A stale event (ADR-106 §C1) leaves the row
        untouched for both GRANTING and EXPIRING.
        """
        row = await self._session.scalar(
            select(Subscription).where(Subscription.user_id == resolved_user_id)
        )
        if row is not None and is_stale(row, event.expires_at):
            return row.status, row.plan, True
        if semantics == parser.SEM_GRANTING:
            status = "active"
            plan = event.vendor_product_id
            if row is None:
                row = Subscription(
                    user_id=resolved_user_id,
                    status=status,
                    plan=plan,
                    expires_at=event.expires_at,
                    will_renew=event.will_renew,
                )
                self._session.add(row)
            else:
                row.status = status
                row.plan = plan
                row.expires_at = event.expires_at
                row.will_renew = event.will_renew
                row.updated_at = _now()
        else:
            # EXPIRING: mark expired, do not touch plan / expires_at / credits.
            status = "expired"
            if row is None:
                row = Subscription(
                    user_id=resolved_user_id,
                    status=status,
                    plan=None,
                    expires_at=None,
                    will_renew=event.will_renew,
                )
                self._session.add(row)
                plan = None
            else:
                row.status = status
                row.will_renew = event.will_renew
                row.updated_at = _now()
                plan = row.plan
        await self._session.flush()
        return status, plan, False

    async def _grant(self, event: ParsedEvent, txn: str, resolved_user_id: uuid.UUID) -> None:
        """Grant credits by product tier — one grant per Apple period on ANY channel (ADR-106 §A).

        With a real ``transaction_id`` the key is ``sub-grant:{T}`` (shared with StoreKit
        ``sync``) and both ``sub-grant:{T}`` and the historical ``adapty-txn:{T}`` are checked
        first. Without it the fallback key ``adapty-txn:{original_transaction_id | event_id}`` is
        unchanged (ADR-047 §C). A taken key with a different amount is «already credited», never a
        non-2xx (§A3). Credits land on ``resolved_user_id`` (ADR-055).
        """
        if event.transaction_id is not None:
            key = f"sub-grant:{event.transaction_id}"
            check_keys: tuple[str, ...] = (key, f"adapty-txn:{event.transaction_id}")
        else:
            key = f"adapty-txn:{txn}"
            check_keys = (key,)
        credits = self._credits_for(event.vendor_product_id)
        granted = await grant_once(
            self._wallet,
            user_id=resolved_user_id,
            amount=credits.amount,
            idempotency_key=key,
            check_keys=check_keys,
            reason="adapty_subscription",
            meta={
                "transactionId": txn,
                "eventType": event.event_type,
                "vendorProductId": event.vendor_product_id,
            },
        )
        if granted is not None and is_product_unmapped(
            event.vendor_product_id, CHANNEL_ADAPTY, credits, settings=self._settings
        ):
            await signal_unmapped_product(
                self._audit,
                logger,
                user_id=resolved_user_id,
                channel=CHANNEL_ADAPTY,
                product_id=event.vendor_product_id,
                amount=credits.amount,
                transaction_id=txn,
            )

    def _credits_for(self, vendor_product_id: str | None) -> SubscriptionCredits:
        """tokens = оверлей продукта -> карта Adapty -> фиксированный фолбэк ЭТОГО канала.

        Пара «карта + фолбэк» у Adapty своя (ADR-029 §5) и с CloudPayments не совпадает:
        переменные калибруются независимо. Вместе с суммой — источник (ADR-106 §D1).
        """
        return subscription_credits(vendor_product_id, CHANNEL_ADAPTY, settings=self._settings)

    def _tier_for(self, vendor_product_id: str | None) -> int:
        """Сумма периода без источника (см. ``_credits_for``)."""
        return self._credits_for(vendor_product_id).amount
