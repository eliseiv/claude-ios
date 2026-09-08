"""Token-purchase routes: POST /v1/tokens/purchase, GET /v1/tokens/products (ADR-015).

Consumable StoreKit IAP -> idempotent credit grant. Distinct from subscription/sync
(auto-renewable): separate endpoint and grant path with meta.source="token_purchase".
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app import instance_config
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

logger = logging.getLogger(__name__)  # == "app.api_gateway.routers.token_purchase"

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
        minor = settings.token_products_price_minor_units
        live = [
            p
            for p in (_from_broadapps(x, token_products, minor_units=minor) for x in data)
            if p is not None
        ]
        if live:
            # Каталогом ЗДЕСЬ владеет поставщик: оверлей уточняет `credits`/`title` уже
            # перечисленных продуктов и НЕ добавляет своих строк — придуманная нами строка не
            # имеет платёжной ссылки и стала бы некликабельной позицией на пейволле.
            return _catalog_response(_refined(live))
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
            # Витриной этой ветки владеем МЫ, поэтому созданные оператором продукты в неё
            # включаются.
            return _catalog_response(_refined(items) + _operator_products())
    # 3) Fallback: token packs derived from TOKEN_PRODUCTS (productId -> credits).
    return _catalog_response(
        _refined(
            [
                TokenProduct(productId=product_id, credits=credits)
                for product_id, credits in settings.token_products().items()
            ]
        )
        + _operator_products()
    )


def _refined(products: list[TokenProduct]) -> list[TokenProduct]:
    """Уточнить перечисленные продукты оверлеем и убрать архивные.

    Фильтр архивных применяется ко ВСЕМ трём веткам источника: «снят с витрины» есть свойство
    ПРОДУКТА, а не свойство источника, из которого строка пришла. На начисления архив не влияет —
    иначе он ломал бы уже оплаченное и активные подписки.

    ⚠️ **Уточнение ПОФИЛДОВОЕ** (ADR-099 §6.1): `credits`/`title` берутся у оверлея только там,
    где он их ЗАДАЛ. Безусловная подстановка обоих полей затёрла бы пустотой живые значения
    источника у строки, созданной правкой одного `archived`, — то есть правка, витрины не
    касавшаяся, убрала бы с пейволла цену и название.
    """
    snapshot = instance_config.get_snapshot()
    refined: list[TokenProduct] = []
    for product in products:
        overlay = snapshot.products.get(product.productId)
        if overlay is None:
            refined.append(product)
            continue
        if overlay.archived:
            continue
        update: dict[str, Any] = {}
        if overlay.tokens is not None:
            update["credits"] = overlay.tokens
        if overlay.name is not None:
            update["title"] = overlay.name
        refined.append(product.model_copy(update=update) if update else product)
    return refined


def _operator_products() -> list[TokenProduct]:
    """Продукты, заведённые оператором: витриной владеем мы, значит показываем их.

    `price`/`currency` остаются пустыми, пока оператор не завёл продукт в панели поставщика:
    «продукт работает» на этом сервисе означает ровно серверную сторону.
    """
    return [
        TokenProduct(
            productId=row.product_id,
            title=row.name,
            kind=(
                "subscription"
                if row.purchase_kind == instance_config.PURCHASE_KIND_SUBSCRIPTION
                else "tokens"
            ),
            credits=row.tokens,
        )
        for row in instance_config.operator_created_rows()
    ]


def _catalog_response(products: list[TokenProduct]) -> TokenProductsResponse:
    """Собрать ответ каталога, подняв `isDefault` у продуктов из `TOKEN_PRODUCTS_DEFAULT`.

    Признак ставится ЗДЕСЬ, в единой точке для всех трёх веток источника, а не в каждой по
    отдельности: ветки различаются тем, ОТКУДА взят перечень продуктов, а «этот продукт
    предвыбран» — свойство продукта, одинаковое для любого источника. Разложив ту же логику по
    трём веткам, мы получили бы три места, где её можно забыть обновить.

    Пересмотр ADR-098 §7 (2026-09-08):
    раньше признак читался из каталога поставщика. Живой каталог broadapps показал, что поля с
    таким смыслом у него НЕТ вовсе — значит источником может быть только наша сторона. Величина,
    которую поставщик не отдаёт, не может прийти от поставщика, сколько её ни жди.

    Предупреждения о нескольких предвыбранных продуктах больше нет намеренно: пока признак
    приходил из чужой панели, несколько помеченных означали ошибку оператора, которую стоило
    показать. Теперь список задаёт оператор ЭТОГО сервиса явной строкой в конфигурации — если он
    перечислил пять, это его решение, а не описка. Предупреждение на каждом запросе каталога
    приучало бы игнорировать предупреждения.
    """
    marked = get_settings().token_products_default()
    if marked:
        products = [
            p.model_copy(update={"isDefault": True}) if p.productId in marked else p
            for p in products
        ]
    return TokenProductsResponse(products=products)


def _from_broadapps(
    item: Any, token_products: dict[str, int], *, minor_units: bool = False
) -> TokenProduct | None:
    """Map one broadapps product dict to a TokenProduct; skip inactive / malformed items.

    ``minor_units=False`` (историческое поведение): price = price_amount с ОТБРОШЕННЫМИ копейками,
    целые рубли ("699.00" -> 699). ``minor_units=True`` — копейки, как и заявляет схема поля
    ("напр. 699 = 6.99"): "699.00" -> 69900, "599.50" -> 59950.

    Почему это флаг, а не безусловное исправление: поле годами отдавало рубли, и приложения,
    которые НЕ делят на 100, показывают верную цену именно на текущем поведении. Включение флага
    у них сделало бы цену стократной. Переход поинстансный, по мере готовности клиента.

    credits приходят из операторской карты TOKEN_PRODUCTS для пакетов; у подписок — null.
    ``isSpecialOffer`` — флаг `is_special_offer` поставщика; отсутствует или не булево => False.
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
            # round(), а не int(): int(6.9899999 * 100) = 698 — двоичное представление
            # десятичной дроби чуть меньше точного значения, и цена молча теряет копейку.
            price = round(float(amount) * 100) if minor_units else int(float(amount))
        except (TypeError, ValueError):
            price = None
    # Флаг спецпредложения. Читается СТРОГО как булево: у поставщика это `true`/`false`, и
    # трактовать «непустую строку» как истину нельзя — тогда, например, "false" из ошибочно
    # сериализованного ответа включило бы предложение вместо того, чтобы его выключить.
    special = item.get("is_special_offer")
    # Признак «продукт по умолчанию» — та же строгость и та же причина, что у флага выше:
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
        isSpecialOffer=special is True,
        # `isDefault` здесь НЕ выставляется: поставщик такого поля не отдаёт, признак поднимает
        # _catalog_response по нашему списку. Чтение несуществующего поля выглядело бы как
        # поддержка, которой нет.
    )
