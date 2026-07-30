"""Token-purchase schemas for /v1/tokens/* (token-purchase/02-api-contracts.md)."""

from __future__ import annotations

import uuid

from pydantic import Field

from app.schemas.common import StrictModel


class TokenPurchaseRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    # Signed StoreKit consumable JWS transaction (compact JWS). Never logged (redaction).
    transaction: str = Field(
        min_length=1,
        description="Подписанная StoreKit-транзакция покупки.",
    )


class TokenPurchaseResponse(StrictModel):
    creditsAdded: int = Field(
        description=(
            "Сколько кредитов начислено этой покупкой. При повторной (уже обработанной) "
            "транзакции — `0` (идемпотентность)."
        )
    )
    newBalance: int = Field(description="Текущий баланс кредитов после покупки.")
    transactionId: str = Field(description="Идентификатор обработанной StoreKit-транзакции.")


class TokenProduct(StrictModel):
    productId: str = Field(description="Идентификатор продукта (совпадает с Adapty/StoreKit).")
    title: str | None = Field(default=None, description="Отображаемое название (или null).")
    kind: str | None = Field(
        default=None, description="`subscription` | `tokens` (или null, если не задан)."
    )
    period: str | None = Field(
        default=None, description="Период подписки (`week`/`year`/…); `null` для токенов."
    )
    price: int | None = Field(
        default=None, description="Цена в минорных единицах (напр. `699` = 6.99), статична."
    )
    currency: str | None = Field(default=None, description="Валюта цены (напр. `RUB`).")
    credits: int | None = Field(
        default=None, description="Кредиты за пакет токенов; `null` для подписки."
    )


class TokenProductsResponse(StrictModel):
    products: list[TokenProduct] = Field(
        description=(
            "Каталог продуктов. Если задан `PRODUCTS_CATALOG` — полный статичный каталог "
            "(подписки+токены) с `title`/`price`/`currency`; иначе только токен-пакеты "
            "(`productId`→`credits`), а цены берёт клиент из Adapty/StoreKit."
        )
    )
