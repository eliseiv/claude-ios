"""RU payment routes under /v1/billing/cloudpayments.

- POST /webhook (ADR-050): called by the payment aggregator (server-to-server), NOT by the iOS
  client. Static bearer auth via the per-route ``require_cloudpayments_webhook`` dependency
  (isolated from the user JWT / admin token / Adapty webhook). The body is read RAW
  (``await request.body()``) with NO Pydantic body model: a malformed callback must yield 2xx,
  never 422 (which the aggregator would retry). Every authorized outcome is HTTP 200 ``{"code": 0}``
  except a real internal failure, which propagates to a 500 (the aggregator retries -> clean
  reprocessing).
- POST /checkout (ADR-051): called by the iOS client (JWT). Creates a payment link via broadapps;
  the ``userId`` sent upstream is the authenticated subject (never the client body), which is the
  key fix for "lost payments". Active only where CLOUDPAYMENTS_APP_ID / CLOUDPAYMENTS_API_TOKEN are
  set (else 503).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api_gateway.rate_limit import enforce_other_limits
from app.billing_cloudpayments.auth import require_cloudpayments_webhook
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.config import Settings, get_settings
from app.deps import (
    CurrentUser,
    get_cloudpayments_checkout_client,
    get_cloudpayments_webhook_service,
)
from app.errors import CloudPaymentsCheckoutNotConfiguredError, RateLimitedError
from app.schemas.billing_cloudpayments import (
    CloudPaymentsCheckoutRequest,
    CloudPaymentsCheckoutResponse,
    CloudPaymentsWebhookResponse,
)

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


@router.post(
    "/checkout",
    response_model=CloudPaymentsCheckoutResponse,
    summary="Создать ссылку на оплату (RU)",
    description=(
        "Создаёт платёжную ссылку для российской оплаты и возвращает `paymentUrl` — откройте его "
        "для оплаты. Требуется авторизация (JWT). Укажите `productId` и `customerEmail`. Доступно "
        "не на всех инсталляциях (`503`, если способ оплаты недоступен)."
    ),
)
async def cloudpayments_checkout(
    body: CloudPaymentsCheckoutRequest,
    current: CurrentUser,
    client: Annotated[CloudPaymentsCheckoutClient, Depends(get_cloudpayments_checkout_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CloudPaymentsCheckoutResponse:
    # userId comes ONLY from the verified JWT subject (never the request body) — the core fix that
    # guarantees the callback (ADR-050) can find this user and credit the right account.
    if not settings.cloudpayments_checkout_configured():
        raise CloudPaymentsCheckoutNotConfiguredError("cloudpayments checkout not configured")
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    client.validate_product(body.productId)
    result = await client.create_payment_link(
        user_id=current.user_id,
        product_id=body.productId,
        customer_email=body.customerEmail,
    )
    return CloudPaymentsCheckoutResponse(
        paymentId=result.payment_id,
        paymentUrl=result.payment_url,
        status=result.status,
        expiresAt=result.expires_at,
    )
