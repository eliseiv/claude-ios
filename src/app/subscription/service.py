"""Subscription service: verify → normalize → upsert → grant → audit (subscription/03)."""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_SUBSCRIPTION_CHANGE, AuditEvent, AuditService
from app.billing_common.single_grant import grant_once, is_stale, signal_unmapped_product
from app.config import get_settings
from app.instance_config import CHANNEL_STOREKIT, is_product_unmapped, subscription_credits
from app.models import Subscription
from app.subscription.storekit import StoreKitVerifier, VerifiedTransaction
from app.wallet.service import WalletService

logger = logging.getLogger(__name__)  # == "app.subscription.service"


@dataclass(frozen=True)
class SubscriptionResult:
    is_subscribed: bool
    expires_at: datetime.datetime | None
    plan: str | None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _row_is_subscribed(row: Subscription) -> bool:
    expires_at = row.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.UTC)
    return row.status == "active" and expires_at > _now()


class SubscriptionService:
    def __init__(
        self,
        session: AsyncSession,
        verifier: StoreKitVerifier,
        wallet: WalletService,
        audit: AuditService,
    ) -> None:
        self._session = session
        self._verifier = verifier
        self._wallet = wallet
        self._audit = audit

    async def sync(self, user_id: uuid.UUID, signed_transaction: str) -> SubscriptionResult:
        """Verify the StoreKit transaction and reconcile subscription + credit grant."""
        verified: VerifiedTransaction = self._verifier.verify(signed_transaction)

        # ADR-106 §C4: a transaction replaced by an upgrade is inactive like a revoked one.
        active = (
            not verified.revoked
            and not verified.upgraded
            and verified.expires_at is not None
            and verified.expires_at > _now()
        )
        status = "active" if active else "expired"

        row = await self._session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        # ADR-106 §C1/§C3: an earlier-period transaction never rewrites a later active row.
        stale = is_stale(row, verified.expires_at)
        if row is None:
            row = Subscription(
                user_id=user_id,
                status=status,
                plan=verified.product_id or None,
                expires_at=verified.expires_at,
            )
            self._session.add(row)
        elif not stale:
            row.status = status
            row.plan = verified.product_id or None
            row.expires_at = verified.expires_at
            row.updated_at = _now()
        await self._session.flush()

        # Grant per period under the key shared with the Adapty webhook (ADR-106 §A); staleness
        # does not decide the grant — the period key does (§C2).
        if active:
            product_id = verified.product_id or None
            settings = get_settings()
            credits = subscription_credits(product_id, CHANNEL_STOREKIT, settings=settings)
            txn = verified.transaction_id
            granted = await grant_once(
                self._wallet,
                user_id=user_id,
                amount=credits.amount,
                idempotency_key=f"sub-grant:{txn}",
                check_keys=(f"sub-grant:{txn}", f"adapty-txn:{txn}"),
                meta={
                    "reason": "subscription_period",
                    "transactionId": txn,
                    "productId": verified.product_id,
                },
                reason="subscription_period",
            )
            if granted is not None and is_product_unmapped(
                product_id, CHANNEL_STOREKIT, credits, settings=settings
            ):
                await signal_unmapped_product(
                    self._audit,
                    logger,
                    user_id=user_id,
                    channel=CHANNEL_STOREKIT,
                    product_id=product_id,
                    amount=credits.amount,
                    transaction_id=txn,
                )

        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_SUBSCRIPTION_CHANGE,
                payload={
                    "status": status,
                    "plan": verified.product_id or None,
                    "transactionId": verified.transaction_id,
                    "environment": verified.environment,
                    "revoked": verified.revoked,
                    "upgraded": verified.upgraded,
                    "stale": stale,
                },
            )
        )

        if stale:
            # §C3: the response is the CURRENT row state, not the stale transaction.
            return SubscriptionResult(
                is_subscribed=_row_is_subscribed(row),
                expires_at=row.expires_at,
                plan=row.plan,
            )
        return SubscriptionResult(
            is_subscribed=active,
            expires_at=verified.expires_at,
            plan=verified.product_id or None,
        )
