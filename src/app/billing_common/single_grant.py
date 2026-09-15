"""Одна оплата стора — одно начисление по любому каналу (ADR-106 §A, §C1, §D3).

Общие для каналов правила, исполняемые на стороне ВЫЗЫВАЮЩЕГО: контракт
``WalletService.grant`` (ADR-005) не меняется.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from app.audit.service import EVENT_SUBSCRIPTION_PRODUCT_UNMAPPED, AuditEvent, AuditService
from app.errors import ConflictError
from app.models import Subscription
from app.observability.logging import log_event
from app.wallet.service import GrantResult, WalletService

SUBSCRIPTION_STATUS_ACTIVE = "active"


async def grant_once(
    wallet: WalletService,
    *,
    user_id: uuid.UUID,
    amount: int,
    idempotency_key: str,
    check_keys: Sequence[str],
    meta: dict[str, Any],
    reason: str,
) -> GrantResult | None:
    """Грант под ``idempotency_key``, если ни один из ``check_keys`` ещё не занят.

    ``None`` — «уже начислено»: занят проверяемый ключ (§A2) либо параллельный канал записал
    строку под тем же ключом с другой суммой (§A3: занятый ключ = начислено независимо от
    суммы). Повтор с той же суммой (``idempotent_replay``) тоже «уже начислено».
    """
    for key in check_keys:
        if await wallet.has_idempotency_key(user_id, key):
            return None
    try:
        result = await wallet.grant(
            user_id=user_id,
            amount=amount,
            idempotency_key=idempotency_key,
            meta=meta,
            reason=reason,
        )
    except ConflictError:
        # Конфликт суммы под занятым ключом — строка уже есть; иной ConflictError не глотаем.
        if await wallet.has_idempotency_key(user_id, idempotency_key):
            return None
        raise
    if result.idempotent_replay:
        return None
    return result


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)


def is_stale(row: Subscription | None, incoming_expires_at: datetime.datetime | None) -> bool:
    """Предикат устаревания ADR-106 §C1 (один для всех путей стора)."""
    return (
        row is not None
        and row.status == SUBSCRIPTION_STATUS_ACTIVE
        and row.expires_at is not None
        and incoming_expires_at is not None
        and _aware(incoming_expires_at) < _aware(row.expires_at)
    )


async def signal_unmapped_product(
    audit: AuditService,
    logger: logging.Logger,
    *,
    user_id: uuid.UUID,
    channel: str,
    product_id: str | None,
    amount: int,
    transaction_id: str,
) -> None:
    """WARNING + audit ``subscription_product_unmapped`` (ADR-106 §D3). Начисление не блокирует."""
    payload: dict[str, Any] = {
        "channel": channel,
        "productId": product_id,
        "amount": amount,
        "transactionId": transaction_id,
    }
    log_event(logger, logging.WARNING, EVENT_SUBSCRIPTION_PRODUCT_UNMAPPED, **payload)
    await audit.record(
        AuditEvent(
            user_id=user_id,
            event_type=EVENT_SUBSCRIPTION_PRODUCT_UNMAPPED,
            payload=payload,
        )
    )
