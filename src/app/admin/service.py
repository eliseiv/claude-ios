"""Admin service (ADR-009, ADM-4/5/6/7): thin wrapper over WalletService.

Does NOT duplicate billing logic — it adds admin authorization context (caller already
passed ``require_admin``), a user-existence check (admin never creates users, ADR-007), an
extra ``admin_grant`` audit event, and the ``admin_grant_total`` metric. Idempotency,
ledger writes and the ``billing_credit`` audit stay in WalletService.grant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_ADMIN_GRANT, AuditEvent, AuditService
from app.errors import ConflictError, UserNotFoundError
from app.models import LedgerTransaction
from app.observability.metrics import admin_grant_total
from app.wallet.service import WalletService


@dataclass(frozen=True)
class AdminGrantResult:
    new_balance: int
    ledger_tx_id: uuid.UUID
    idempotent_replay: bool


@dataclass(frozen=True)
class AdminWalletView:
    user_id: uuid.UUID
    balance: int
    last_transactions: list[LedgerTransaction]


class AdminService:
    def __init__(self, session: AsyncSession, wallet: WalletService, audit: AuditService) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit

    async def _require_user_exists(self, user_id: uuid.UUID) -> None:
        """Admin grant/view never creates users — missing userId is a 404 (ADR-009, ADR-007).

        Parameterized lookup. Done BEFORE WalletService.grant (which would _ensure_wallet but
        never create the users row) so an operator typo surfaces as user_not_found, not a silent
        phantom account.
        """
        exists = await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :uid"),
            {"uid": str(user_id)},
        )
        if exists is None:
            admin_grant_total.labels(result="not_found").inc()
            raise UserNotFoundError("user not found")

    async def grant(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        reason: str,
    ) -> AdminGrantResult:
        """Credit a user's wallet on behalf of an operator (saap/compensation).

        Reuses WalletService.grant verbatim (atomic, idempotent by (user_id, idempotency_key),
        writes ledger credit + billing_credit audit). Adds an admin_grant audit event recording
        the admin initiation (actor=admin, reason). The X-Admin-Token secret is never part of any
        payload. A reused key with a different amount surfaces as 409 (conflict).
        """
        await self._require_user_exists(user_id)
        meta: dict[str, Any] = {"source": "admin", "reason": reason}
        try:
            result = await self._wallet.grant(
                user_id=user_id,
                amount=amount,
                idempotency_key=idempotency_key,
                meta=meta,
                reason=reason,
            )
        except ConflictError:
            admin_grant_total.labels(result="conflict").inc()
            raise

        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_ADMIN_GRANT,
                payload={
                    "actor": "admin",
                    "userId": str(user_id),
                    "amount": amount,
                    "reason": reason,
                    "idempotencyKey": idempotency_key,
                    "ledgerTxId": str(result.ledger_tx_id),
                    "idempotentReplay": result.idempotent_replay,
                },
            )
        )
        admin_grant_total.labels(result="success").inc()
        return AdminGrantResult(
            new_balance=result.new_balance,
            ledger_tx_id=result.ledger_tx_id,
            idempotent_replay=result.idempotent_replay,
        )

    async def get_wallet_view(self, user_id: uuid.UUID, last_n: int) -> AdminWalletView:
        """Read-only wallet view for support. Missing userId → 404 (never creates a user)."""
        await self._require_user_exists(user_id)
        balance, txs = await self._wallet.get_wallet_view(user_id, last_n)
        return AdminWalletView(user_id=user_id, balance=balance, last_transactions=txs)
