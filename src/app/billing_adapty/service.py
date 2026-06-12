"""AdaptyWebhookService: defensive parse -> dedup -> upsert subscription -> grant -> audit.

Implements ADR-029 §2-§7 / billing-adapty/03-architecture.md. ``handle(raw)`` never raises on a
malformed / unrecognised payload — it returns an ``ignored`` / ``duplicate`` / ``applied`` outcome
(the router maps every one of these to HTTP 200). It DOES raise on a real internal failure (e.g.
the DB is unavailable): the caller's session_scope then rolls the whole transaction back and the
router surfaces 500, which Adapty retries — on retry ``event_id`` is free again (the INSERT was
rolled back) so reprocessing is clean (and ``grant`` is additionally idempotent by key).

Idempotency lives in a single statement: ``INSERT adapty_webhook_events ... ON CONFLICT (event_id)
DO NOTHING RETURNING event_id``. Empty RETURNING => duplicate => no mutations. Otherwise the
subscription upsert + (for started/renewed) credit grant + audit run in the SAME transaction.
"""

from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_ADAPTY_SUBSCRIPTION, AuditEvent, AuditService
from app.billing_adapty import parser
from app.billing_adapty.parser import ParsedEvent
from app.config import Settings
from app.models import Subscription
from app.wallet.service import WalletService


@dataclass(frozen=True)
class WebhookOutcome:
    """Result of handling one webhook call. The router maps it to an HTTP-200 JSON envelope."""

    result: str  # "ignored" | "duplicate" | "applied"
    reason: str | None = None
    event_type: str | None = None


def _ignored(reason: str | None = None, event_type: str | None = None) -> WebhookOutcome:
    return WebhookOutcome(result="ignored", reason=reason, event_type=event_type)


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

    async def handle(self, raw: bytes) -> WebhookOutcome:
        """Process one raw webhook body. Always returns a 200-mappable outcome unless the DB fails.

        Pre-transaction validation (empty / not-JSON / not-object / missing id / missing user /
        user-not-found / unknown type) yields ``ignored`` with no DB writes. A recognised event is
        applied inside the caller's transaction; any real DB failure propagates (=> rollback/500).
        """
        # --- Stage 1: body shape (no DB) ---
        if not raw:
            return _ignored("empty_body")
        try:
            body: Any = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return _ignored("invalid_json")
        if not isinstance(body, dict):
            return _ignored("not_an_object")

        # --- Stage 2: defensive field parsing (no DB) ---
        event_id = parser.parse_event_id(body)
        if event_id is None:
            return _ignored("missing_event_id")
        customer_user_id = parser.parse_customer_user_id(body)
        if customer_user_id is None:
            # Absent or non-UUID customer_user_id is equivalent to "user not found" per contract,
            # but the contract distinguishes the reason: a missing/non-UUID id is reported as
            # missing_customer_user_id (02-api-contracts.md), whereas a well-formed UUID with no
            # matching users row is user_not_found.
            return _ignored("missing_customer_user_id")
        event_type = parser.parse_event_type(body)

        # --- Stage 3: user existence (DB read; we never provision users here) ---
        if not await self._user_exists(customer_user_id):
            return _ignored("user_not_found")

        # --- Stage 4: event-type dispatch ---
        if event_type not in parser.KNOWN_EVENTS:
            # Echo the (normalised) event_type so operators can see what arrived. No audit, no
            # mutation: an unknown type is "no event happened" (architect-reviewer minor).
            return _ignored(event_type=event_type)

        parsed = ParsedEvent(
            event_id=event_id,
            event_type=event_type,
            customer_user_id=customer_user_id,
            vendor_product_id=parser.parse_vendor_product_id(body),
            expires_at=parser.parse_expires_at(body),
        )
        return await self._apply(parsed, body)

    async def _user_exists(self, user_id: uuid.UUID) -> bool:
        exists = await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :uid"),
            {"uid": str(user_id)},
        )
        return exists is not None

    async def _apply(self, event: ParsedEvent, body: dict[str, Any]) -> WebhookOutcome:
        """Apply a recognised event inside the caller's single transaction (ADR-029 §6).

        The INSERT ... ON CONFLICT DO NOTHING RETURNING is the sole dedup point: empty RETURNING
        means a concurrent/previous delivery already recorded this event_id -> duplicate, no
        mutations. Otherwise upsert the subscription, grant credits for started/renewed, and write
        the audit row — all in this transaction, committed by session_scope on success.
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
                "uid": str(event.customer_user_id),
                "event_type": event.event_type,
                "payload": json.dumps(body),
            },
        )
        if inserted is None:
            # Duplicate event_id: no mutations (ADR-029 §6, architect-reviewer minor).
            return WebhookOutcome(result="duplicate")

        status, plan = await self._upsert_subscription(event)

        if event.event_type in parser.GRANTING_EVENTS:
            await self._grant(event)

        await self._audit.record(
            AuditEvent(
                user_id=event.customer_user_id,
                event_type=EVENT_ADAPTY_SUBSCRIPTION,
                payload={
                    "adaptyEventId": event.event_id,
                    "eventType": event.event_type,
                    "status": status,
                    "plan": plan,
                    "expiresAt": event.expires_at.isoformat() if event.expires_at else None,
                    "customerId": str(event.customer_user_id),
                },
            )
        )
        return WebhookOutcome(result="applied")

    async def _upsert_subscription(self, event: ParsedEvent) -> tuple[str, str | None]:
        """Upsert subscriptions per the event mapping (ADR-029 §4). Returns (status, plan).

        started/renewed -> active, plan=vendor_product_id, expires_at (if present).
        cancelled/expired -> expired; plan and expires_at are left unchanged.
        """
        row = await self._session.scalar(
            select(Subscription).where(Subscription.user_id == event.customer_user_id)
        )
        if event.event_type in parser.GRANTING_EVENTS:
            status = "active"
            plan = event.vendor_product_id
            if row is None:
                row = Subscription(
                    user_id=event.customer_user_id,
                    status=status,
                    plan=plan,
                    expires_at=event.expires_at,
                )
                self._session.add(row)
            else:
                row.status = status
                row.plan = plan
                row.expires_at = event.expires_at
                row.updated_at = _now()
        else:
            # cancelled / expired: mark expired, do not touch plan / expires_at / credits.
            status = "expired"
            if row is None:
                row = Subscription(
                    user_id=event.customer_user_id,
                    status=status,
                    plan=None,
                    expires_at=None,
                )
                self._session.add(row)
                plan = None
            else:
                row.status = status
                row.updated_at = _now()
                plan = row.plan
        await self._session.flush()
        return status, plan

    async def _grant(self, event: ParsedEvent) -> None:
        """Grant credits by product tier, idempotent by ``adapty-event:{event_id}`` (ADR-029 §5)."""
        tier = self._tier_for(event.vendor_product_id)
        await self._wallet.grant(
            user_id=event.customer_user_id,
            amount=tier,
            idempotency_key=f"adapty-event:{event.event_id}",
            reason="adapty_subscription",
            meta={
                "adaptyEventId": event.event_id,
                "eventType": event.event_type,
                "vendorProductId": event.vendor_product_id,
            },
        )

    def _tier_for(self, vendor_product_id: str | None) -> int:
        """tokens = product-tier map[vendor_product_id] or the fixed fallback grant (ADR-029 §5)."""
        mapped = self._settings.adapty_product_tokens().get(vendor_product_id or "")
        return mapped or self._settings.adapty_subscription_tokens_grant
