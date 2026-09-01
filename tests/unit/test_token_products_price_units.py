"""Единицы поля `price` в GET /v1/tokens/products.

Схема поля годами заявляет минорные единицы («напр. 699 = 6.99»), а ветка broadapps кладёт туда
ЦЕЛЫЕ РУБЛИ, отбрасывая копейки. Приложение, делящее на 100 ПО КОНТРАКТУ, показывает цену в сто
раз меньше: недельная подписка за 599 ₽ выглядит как 5,99 ₽ (прод qoravena, 2026-09-01).

Исправление сделано флагом, а не безусловно, и тест закрепляет ОБЕ стороны. Приложения других
инстансов на 100 не делят — для них верна именно историческая ветка, и безусловное исправление
показало бы им стократную цену. Тест, проверяющий только новое поведение, позволил бы позже
«прибраться», выкинув старую ветку, и сломать их молча.
"""

from __future__ import annotations

import pytest

from app.api_gateway.routers.token_purchase import _from_broadapps

_TOKENS = {"100_Tokens_9.99": 100}


def _item(amount: str, code: str = "week_6.99_not_trial", **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "code": code,
        "price_amount": amount,
        "price_currency": "RUB",
        "payment_type": "subscription",
        "subscription_interval_unit": "week",
    }
    base.update(kw)
    return base


@pytest.mark.parametrize(
    ("amount", "major", "minor"),
    [
        ("599.00", 599, 59900),
        ("3490.00", 3490, 349000),
        # Копейки НЕ теряются в минорном режиме — ради них он и вводится.
        ("599.50", 599, 59950),
        ("6.99", 6, 699),
    ],
)
def test_units_switch(amount: str, major: int, minor: int) -> None:
    assert _from_broadapps(_item(amount), _TOKENS).price == major
    assert _from_broadapps(_item(amount), _TOKENS, minor_units=True).price == minor


@pytest.mark.parametrize(("amount", "expected"), [("19.99", 1999), ("8.29", 829), ("1.15", 115)])
def test_kopeck_is_not_lost_to_binary_representation(amount: str, expected: int) -> None:
    """Усечение отнимает копейку там, где двоичная дробь чуть МЕНЬШЕ точного значения.

    `19.99 * 100` в float равно 1998.9999999999998, поэтому `int()` даёт 1998. Отказ молчаливый:
    цена просто оказывается на копейку ниже, и заметить это можно лишь сверив все продукты
    вручную. Значения подобраны те, где ловушка СРАБАТЫВАЕТ: на «6.99» умножение точное, и тест
    на нём прошёл бы при любой реализации, ничего не проверив.
    """
    assert int(float(amount) * 100) == expected - 1  # ловушка воспроизводится
    assert _from_broadapps(_item(amount), _TOKENS, minor_units=True).price == expected


def test_switch_touches_nothing_but_price() -> None:
    """Флаг меняет ЕДИНИЦЫ цены и больше ничего — иначе он станет тихим переключателем поведения."""
    a = _from_broadapps(_item("599.00"), _TOKENS)
    b = _from_broadapps(_item("599.00"), _TOKENS, minor_units=True)
    assert a is not None and b is not None
    ignore = {"price"}
    assert a.model_dump(exclude=ignore) == b.model_dump(exclude=ignore)


def test_missing_price_stays_null_in_both_modes() -> None:
    """Нет цены — значит нет; в минорных единицах ноль был бы враньём «бесплатно»."""
    for minor in (False, True):
        item = _item("x")
        item.pop("price_amount")
        assert _from_broadapps(item, _TOKENS, minor_units=minor).price is None
