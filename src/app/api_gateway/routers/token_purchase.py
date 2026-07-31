"""Token-purchase routes: POST /v1/tokens/purchase, GET /v1/tokens/products (ADR-015).

Consumable StoreKit IAP -> idempotent credit grant. Distinct from subscription/sync
(auto-renewable): separate endpoint and grant path with meta.source="token_purchase".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app.api_gateway.rate_limit import enforce_other_limits
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.config import get_settings
from app.deps import (
    CurrentUser,
    get_cloudpayments_checkout_client,
    get_token_purchase_service,
    require_owner,
)
from app.errors import RateLimitedError
from app.schemas.token_purchase import (
    TokenProduct,
    TokenProductsResponse,
    TokenPurchaseRequest,
    TokenPurchaseResponse,
)
from app.token_purchase.service import TokenPurchaseService

router = APIRouter(prefix="/v1/tokens", tags=["Tokens"])


@router.post(
    "/purchase",
    response_model=TokenPurchaseResponse,
    summary="Купить пакет токенов",
    description=(
        "Пришлите подписанную StoreKit-транзакцию в поле `transaction`. Начисляет кредиты по "
        "`productId`. Повторная отправка той же транзакции не начисляет дважды "
        "(`creditsAdded=0`). Неизвестный `productId` или поддельная транзакция — `422`. "
        "Требует активной подписки, иначе `403 {code: subscription_required}`."
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
        "Возвращает пакеты токенов: `productId` и число кредитов. Цены отображает клиент из "
        "StoreKit."
    ),
)
async def list_token_products(
    current: CurrentUser,
    client: Annotated[CloudPaymentsCheckoutClient, Depends(get_cloudpayments_checkout_client)],
) -> TokenProductsResponse:
    settings = get_settings()
    # 1) Live catalog from broadapps (source of truth for RU products). credits come from our
    #    TOKEN_PRODUCTS map (broadapps does not know credit amounts); subscriptions -> null.
    data = await client.list_products()
    if data:
        token_products = settings.token_products()
        live = [p for p in (_from_broadapps(x, token_products) for x in data) if p is not None]
        if live:
            return TokenProductsResponse(products=live)
    # 2) Fallback: static PRODUCTS_CATALOG (skip items that fail schema validation).
    catalog = settings.products_catalog()
    if catalog:
        items: list[TokenProduct] = []
        for raw in catalog:
            try:
                items.append(TokenProduct.model_validate(raw))
            except ValidationError:
                continue
        if items:
            return TokenProductsResponse(products=items)
    # 3) Fallback: token packs derived from TOKEN_PRODUCTS (productId -> credits).
    return TokenProductsResponse(
        products=[
            TokenProduct(productId=product_id, credits=credits)
            for product_id, credits in settings.token_products().items()
        ]
    )


def _from_broadapps(item: Any, token_products: dict[str, int]) -> TokenProduct | None:
    """Map one broadapps product dict to a TokenProduct; skip inactive / malformed items.

    price = price_amount (major units, e.g. "699.00") -> minor units int (69900). credits come from
    the operator TOKEN_PRODUCTS map for token packs; subscriptions carry null credits.
    """
    if not isinstance(item, dict):
        return None
    code = item.get("code")
    if not isinstance(code, str) or not code:
        return None
    if item.get("status") not in (None, "active"):
        return None
    is_sub = item.get("payment_type") == "subscription"
    price: int | None = None
    amount = item.get("price_amount")
    if isinstance(amount, str | int | float):
        try:
            price = round(float(amount) * 100)
        except (TypeError, ValueError):
            price = None
    period = item.get("subscription_interval_unit")
    currency = item.get("price_currency")
    name = item.get("name")
    return TokenProduct(
        productId=code,
        title=name if isinstance(name, str) else None,
        kind="subscription" if is_sub else "tokens",
        period=period if isinstance(period, str) else None,
        price=price,
        currency=currency if isinstance(currency, str) else None,
        credits=None if is_sub else token_products.get(code),
    )
