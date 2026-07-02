"""RU payment webhook route: POST /v1/billing/cloudpayments/webhook (ADR-050).

Called by the payment aggregator (server-to-server), NOT by the iOS client. Static bearer auth via
the per-route ``require_cloudpayments_webhook`` dependency (isolated from the user JWT / admin token
/ Adapty webhook). The body is read RAW (``await request.body()``) with NO Pydantic body model: a
malformed callback must yield 2xx, never 422 (which the aggregator would retry). Every authorized
outcome is HTTP 200 ``{"code": 0}`` except a real internal failure, which propagates to a 500 (the
aggregator retries -> clean reprocessing).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.billing_cloudpayments.auth import require_cloudpayments_webhook
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.deps import get_cloudpayments_webhook_service
from app.schemas.billing_cloudpayments import CloudPaymentsWebhookResponse

router = APIRouter(prefix="/v1/billing/cloudpayments", tags=["Billing (CloudPayments)"])


@router.post(
    "/webhook",
    response_model=CloudPaymentsWebhookResponse,
    dependencies=[Depends(require_cloudpayments_webhook)],
    summary="Приём платежа RU (webhook)",
    description=(
        "Серверный вебхук платёжного агрегатора (вызывает агрегатор, не клиент). Авторизация — "
        "статический `Authorization: Bearer <секрет>` (constant-time). Тело читается сырым, без "
        'валидации схемы. После авторизации ответ всегда `200` с телом `{"code": 0}` (событие '
        "принято: платёж начислен либо проигнорирован). `500` — только при незаданном секрете или "
        "реальном сбое БД (тогда агрегатор повторяет доставку)."
    ),
)
async def cloudpayments_webhook(
    request: Request,
    service: Annotated[CloudPaymentsWebhookService, Depends(get_cloudpayments_webhook_service)],
) -> JSONResponse:
    raw = await request.body()
    # The outcome (applied | duplicate | ignored/*) is emitted to logs/audit by the service; the
    # aggregator receives only {"code": 0} for every processed callback (ADR-050 §6).
    await service.handle(raw)
    return JSONResponse({"code": 0}, status_code=200)
