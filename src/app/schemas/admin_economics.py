"""Схемы контракта CRM v1.1 + v1.2 + v1.4 + v1.5 (ADR-099).

⚠️ **Контракт заморожен НЕ здесь.** Пути, имена полей, значения перечислений и коды ответов
задала CRM; из этого репозитория они не меняются: обе стороны пишут разные команды в разных
репозиториях, и расхождение в имени поля даёт у оператора МОЛЧА ПУСТОЙ ЭКРАН, а не ошибку.

Формат — **snake_case** (контракт CRM), в отличие от пользовательского camelCase API.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import StrictModel

PurchaseKind = Literal["subscription", "one_time"]


class AdminProductItem(StrictModel):
    """Строка каталога продуктов: то, за что инстанс начисляет кредиты."""

    product_id: str = Field(description="Идентификатор продукта в сторе или панели поставщика.")
    name: str = Field(description="Отображаемое название.")
    price: str | None = Field(
        default=None, description="Витринная цена; этот сервис достоверного прайса не имеет."
    )
    period: str | None = Field(default=None, description="Витринный период подписки.")
    tokens: int | None = Field(default=None, description="Кредиты, начисляемые по продукту.")
    avatar_tokens: int | None = Field(
        default=None, description="Второй валюты сервис не ведёт: значение всегда пустое."
    )
    grantable: bool = Field(default=True, description="Может ли оператор выдать продукт вручную.")
    purchase_kind: PurchaseKind | None = Field(
        default=None, description="Класс продукта: подписка или разовая покупка."
    )
    archived: bool = Field(default=False, description="Снят ли продукт с витрины приложения.")
    updated_at: str | None = Field(
        default=None, description="Пусто — строку ни разу не меняли из панели."
    )


class AdminProductListResponse(StrictModel):
    items: list[AdminProductItem]


class AdminProductCreateRequest(StrictModel):
    product_id: str = Field(
        min_length=1,
        max_length=128,
        description="Идентификатор, заведённый оператором в сторе или панели поставщика.",
    )
    name: str = Field(min_length=1, max_length=255, description="Отображаемое название.")
    purchase_kind: PurchaseKind = Field(description="Подписка или разовая покупка.")
    # `ge=0` — реализация границы САМОГО контракта (`tokens: int >= 0`), поэтому она живёт в
    # схеме ручки, а не в сервисе: отказ отдаёт штатный конвейер FastAPI тем же `422`, что и на
    # отсутствующем поле. В сервисе остаётся ровно одна проверка нижней границы — НАША
    # собственная (`one_time >= 1`), и она `400` (ADR-099 §11).
    tokens: int = Field(ge=0, description="Кредиты за покупку или за период подписки.")
    avatar_tokens: int | None = Field(
        default=None,
        description="Не поддерживается: сервис не ведёт вторую валюту, значение отвергается.",
    )


class AdminProductPatchRequest(StrictModel):
    # `ge=0` — граница самого контракта; наша собственная нижняя граница класса живёт в
    # сервисе и отвечает `400` (ADR-099 §11).
    tokens: int | None = Field(default=None, ge=0, description="Новое число кредитов.")
    archived: bool | None = Field(default=None, description="Снять продукт с витрины или вернуть.")
    avatar_tokens: int | None = Field(
        default=None,
        description="Не поддерживается: сервис не ведёт вторую валюту, значение отвергается.",
    )
    if_updated_at: datetime.datetime | None = Field(
        default=None, description="Отметка версии строки, на которой основана правка."
    )


class AdminProductCreateResponse(AdminProductItem):
    effective_after_seconds: int = Field(
        description="Через сколько секунд продукт станет виден всем процессам инстанса."
    )


class AdminProductWriteResponse(AdminProductItem):
    previous_tokens: int | None = Field(default=None, description="Значение до правки.")
    changed: bool = Field(default=True, description="Изменилось ли что-нибудь фактически.")
    effective_after_seconds: int = Field(
        description="Через сколько секунд правка применится во всех процессах инстанса."
    )


class AdminTariffItem(StrictModel):
    """Строка тарифа: сколько кредитов стоит одна единица, названная в `unit`."""

    tariff_id: str = Field(description="Ключ строки для правки.")
    kind: Literal["chat", "photo", "video"] = Field(description="Поверхность тарификации.")
    name: str | None = Field(default=None, description="Человекочитаемая подпись варианта.")
    tokens: int = Field(description="Кредиты за одну единицу, названную в `unit`.")
    provider: str | None = Field(default=None, description="Поставщик, обслуживающий вариант.")
    model: str | None = Field(default=None, description="Идентификатор модели.")
    unit: Literal["message", "image", "generation"] = Field(
        description="Единица, за которую назначена цена."
    )
    options: dict[str, Any] | None = Field(
        default=None, description="Прочие измерения цены варианта."
    )
    updated_at: str | None = Field(
        default=None, description="Пусто — строку ни разу не меняли из панели."
    )


class AdminTariffListResponse(StrictModel):
    items: list[AdminTariffItem]


class AdminTariffPatchRequest(StrictModel):
    # `ge=0` — граница самого контракта (`number >= 0`); НАША нижняя граница (`>= 1`) в
    # объявлении невыразима и отвечает `400` + `undeclared_bound` из сервиса (ADR-099 §11).
    tokens: int = Field(ge=0, description="Новая цена в кредитах, целое число не меньше единицы.")
    if_updated_at: datetime.datetime | None = Field(
        default=None, description="Отметка версии строки, на которой основана правка."
    )


class AdminTariffWriteResponse(AdminTariffItem):
    previous_tokens: int | None = Field(default=None, description="Значение до правки.")
    changed: bool = Field(default=True, description="Изменилось ли что-нибудь фактически.")
    effective_after_seconds: int = Field(
        description="Через сколько секунд правка применится во всех процессах инстанса."
    )


class AdminSettingOption(StrictModel):
    value: str
    label: str


class AdminSettingItem(StrictModel):
    """Продуктовая настройка инстанса. Поверхность самоописываема: тип и допустимые значения
    приходят в ответе, потребитель их не хардкодит."""

    setting_id: str = Field(description="Ключ строки для правки.")
    type: Literal["bool", "enum", "multi_enum", "string"] = Field(description="Тип значения.")
    label: str = Field(description="Подпись для формы оператора.")
    value: Any = Field(description="Действующее значение, по объявленному типу.")
    group: str | None = Field(default=None, description="Раздел формы.")
    description: str | None = Field(default=None, description="Пояснение для оператора.")
    options: list[AdminSettingOption] | None = Field(
        default=None, description="Допустимые значения; пусто — набор не ограничен."
    )
    constraints: dict[str, int] | None = Field(
        default=None, description="Объявленные границы значения; отсутствующий ключ не проверяется."
    )
    readonly: bool | None = Field(default=None, description="Доступна ли строка только на чтение.")
    updated_at: str | None = Field(
        default=None, description="Пусто — строку ни разу не меняли из панели."
    )


class AdminSettingListResponse(StrictModel):
    items: list[AdminSettingItem]


class AdminSettingPatchRequest(StrictModel):
    value: Any = Field(description="Новое значение, по объявленному типу строки.")
    if_updated_at: datetime.datetime | None = Field(
        default=None, description="Отметка версии строки, на которой основана правка."
    )


class AdminSettingWriteResponse(AdminSettingItem):
    previous_value: Any = Field(default=None, description="Значение до правки.")
    changed: bool = Field(default=True, description="Изменилось ли что-нибудь фактически.")
    effective_after_seconds: int = Field(
        description="Через сколько секунд правка применится во всех процессах инстанса."
    )


class AdminCapabilitiesResponse(StrictModel):
    """Что этот инстанс умеет. `features` — единственный источник права записи для панели."""

    contract_version: int = Field(description="Версия объявления возможностей.")
    features: list[str] = Field(description="Реализованные возможности; отсутствие = запрет.")
    limits: dict[str, int] = Field(
        description="Границы величин этого инстанса; отсутствующий ключ означает «величины нет»."
    )
    cache_effective_after_seconds: int = Field(
        description="Через сколько секунд правка применяется во всех процессах инстанса."
    )
