"""Флаг спецпредложения из рублёвого каталога (`is_special_offer` -> `isSpecialOffer`).

Проверяется не «поле есть», а три вещи, каждая из которых ломается молча:

1. ПРОБРОС. Поставщик добавил флаг в свой каталог; без отображения он теряется на границе, и
   приложение не может выделить предложение — при этом ни ошибки, ни отказа не возникает.
2. СТРОГАЯ БУЛЕВОСТЬ. У поставщика это `true`/`false`. Трактовка «непустое значение = истина»
   включила бы предложение на строке `"false"` — то есть ровно там, где его надо выключить.
3. ДЕФОЛТ. На инстансах без рублёвого каталога поля нет вовсе. Оно обязано быть `false`, а не
   отсутствовать: клиент читает его безусловно, и `null` заставил бы каждого клиента заводить
   свою трактовку отсутствия.
"""

from __future__ import annotations

import pytest

from app.api_gateway.routers.token_purchase import _from_broadapps
from app.schemas.token_purchase import TokenProduct

_TOKENS = {"100_Tokens_9.99": 100}


def _item(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "code": "week_6.99_nottrial",
        "price_amount": "599.00",
        "price_currency": "RUB",
        "payment_type": "subscription",
        "subscription_interval_unit": "week",
    }
    base.update(kw)
    return base


def test_flag_is_passed_through() -> None:
    assert _from_broadapps(_item(is_special_offer=True), _TOKENS).isSpecialOffer is True
    assert _from_broadapps(_item(is_special_offer=False), _TOKENS).isSpecialOffer is False


@pytest.mark.parametrize("raw", ["false", "true", "", 0, 1, None, "yes", []])
def test_only_a_real_boolean_true_enables_it(raw: object) -> None:
    """Строка `"false"` — самый опасный вход: она истинна как значение и ложна по смыслу."""
    assert _from_broadapps(_item(is_special_offer=raw), _TOKENS).isSpecialOffer is False


def test_absent_field_means_not_special() -> None:
    """Каталог без этого поля (старый поставщик, статический каталог) — не спецпредложение."""
    assert _from_broadapps(_item(), _TOKENS).isSpecialOffer is False


def test_default_is_false_not_null() -> None:
    """Поле есть ВСЕГДА: клиент читает его безусловно, не проверяя на отсутствие."""
    assert TokenProduct(productId="x").isSpecialOffer is False


def test_flag_does_not_disturb_anything_else() -> None:
    """Флаг отображательный: ни цена, ни начисление, ни вид продукта от него не зависят."""
    on = _from_broadapps(_item(is_special_offer=True), _TOKENS)
    off = _from_broadapps(_item(is_special_offer=False), _TOKENS)
    assert on.model_dump(exclude={"isSpecialOffer"}) == off.model_dump(exclude={"isSpecialOffer"})
