"""Token-purchase routes: POST /v1/tokens/purchase, GET /v1/tokens/products (ADR-015).

Consumable StoreKit IAP -> idempotent credit grant. Distinct from subscription/sync
(auto-renewable): separate endpoint and grant path with meta.source="token_purchase".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.openapi_security import bearer_scheme
from app.api_gateway.rate_limit import enforce_other_limits
from app.config import get_settings
from app.deps import CurrentUser, get_token_purchase_service, require_owner
from app.errors import RateLimitedError
from app.schemas.token_purchase import (
    TokenProduct,
    TokenProductsResponse,
    TokenPurchaseRequest,
    TokenPurchaseResponse,
)
from app.token_purchase.service import TokenPurchaseService

router = APIRouter(prefix="/v1/tokens", tags=["Tokens"], dependencies=[Depends(bearer_scheme)])


@router.post(
    "/purchase",
    response_model=TokenPurchaseResponse,
    summary="Купить пакет токенов",
    description=(
        "Принимает подписанную StoreKit consumable-транзакцию (JWS), проверяет её подпись и "
        "начисляет кредиты по серверному маппингу `productId → credits` (`TOKEN_PRODUCTS`). "
        "Идемпотентно по `transactionId`: повторная отправка той же транзакции не начислит "
        "повторно (`creditsAdded=0`). Неизвестный `productId` или поддельная транзакция → "
        "`422`. Поле `transaction` не логируется (redaction). Требует активной подписки "
        "(Q-015-1=B, ADR-015): без неё — `403 {code: subscription_required}`, начисление не "
        "выполняется."
    ),
)
async def purchase_tokens(
    body: TokenPurchaseRequest,
    request: Request,
    current: CurrentUser,
    service: Annotated[TokenPurchaseService, Depends(get_token_purchase_service)],
) -> TokenPurchaseResponse:
    require_owner(body.userId, current)
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    result = await service.purchase(current.user_id, body.transaction)
    return TokenPurchaseResponse(
        creditsAdded=result.credits_added,
        newBalance=result.new_balance,
        transactionId=result.transaction_id,
    )


@router.get(
    "/products",
    response_model=TokenProductsResponse,
    summary="Каталог пакетов токенов",
    description=(
        "Возвращает серверный маппинг пакетов токенов (`productId → credits`) из "
        "`TOKEN_PRODUCTS`. Цены отображает клиент из StoreKit; backend отдаёт только число "
        "кредитов на пакет."
    ),
)
async def list_token_products(
    current: CurrentUser,
) -> TokenProductsResponse:
    products = get_settings().token_products()
    return TokenProductsResponse(
        products=[
            TokenProduct(productId=product_id, credits=credits)
            for product_id, credits in products.items()
        ]
    )
