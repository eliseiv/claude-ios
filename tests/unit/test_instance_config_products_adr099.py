"""Unit: каталог продуктов и разрешение начислений (ADR-099 §6).

**Несущее требование файла — «ни одно начисление не меняется».** Каналов подписки ЧЕТЫРЕ, и у
каждого своя пара «карта продукта → фолбэк». Фикстура задаёт три фолбэка РАЗНЫМИ числами
намеренно: на совпадающих дефолтах (все 1000) подмена одного фолбэка другим невидима, и кейс
прошёл бы при неверной реализации.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.config import Settings
from app.instance_config.products import (
    CHANNEL_ADAPTY,
    CHANNEL_CLOUDPAYMENTS,
    CHANNEL_MANUAL,
    CHANNEL_STOREKIT,
    PURCHASE_KIND_ONE_TIME,
    PURCHASE_KIND_SUBSCRIPTION,
    SOURCE_ADAPTY,
    SOURCE_CLOUDPAYMENTS,
    SOURCE_OPERATOR,
    SOURCE_PRODUCTS_CATALOG,
    SOURCE_TOKEN_PRODUCTS,
    catalog_rows,
    find_product,
    is_archived,
    known_product_ids,
    one_time_credits,
    operator_created_rows,
    subscription_credits,
)
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    ProductOverlay,
)

_NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.UTC)

# Продукты, которых нет НИ В ОДНОЙ карте канала: именно они уходят в фолбэк, и именно на них
# подмена фолбэка становится видимой.
_UNMAPPED = "com.example.plan.unmapped"

_ONE_TIME_ID = "tokens_100"
_ADAPTY_ID = "adapty.sub.month"
_CLOUDPAYMENTS_ID = "cp.sub.month"
_CATALOG_ONLY_ID = "showcase.only"

# ТРИ РАЗНЫХ числа. Совпадение сегодняшних дефолтов (все 1000) ничего не гарантирует на проде:
# `config.py` прямо помечает эти величины как калибруемые независимо.
_CP_FALLBACK = 111
_ADAPTY_FALLBACK = 222
_PERIOD_FALLBACK = 333
_CP_MAPPED = 444
_ADAPTY_MAPPED = 555


def _settings(**kwargs: Any) -> Settings:
    return Settings(
        **{
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "TOKEN_PRODUCTS": f'{{"{_ONE_TIME_ID}": 100}}',
            "ADAPTY_PRODUCT_TOKENS": f'{{"{_ADAPTY_ID}": {_ADAPTY_MAPPED}}}',
            "CLOUDPAYMENTS_PRODUCT_TOKENS": f'{{"{_CLOUDPAYMENTS_ID}": {_CP_MAPPED}}}',
            "PRODUCTS_CATALOG": (
                f'[{{"productId": "{_CATALOG_ONLY_ID}", "title": "Показ", "kind": "subscription",'
                ' "credits": 700}]'
            ),
            "CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT": _CP_FALLBACK,
            "ADAPTY_SUBSCRIPTION_TOKENS_GRANT": _ADAPTY_FALLBACK,
            "SUBSCRIPTION_CREDITS_PER_PERIOD": _PERIOD_FALLBACK,
            **kwargs,
        }
    )


def _overlay(
    product_id: str,
    *,
    tokens: int,
    kind: str = PURCHASE_KIND_SUBSCRIPTION,
    name: str = "Оператор",
    archived: bool = False,
) -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        products={
            product_id: ProductOverlay(
                product_id=product_id,
                name=name,
                purchase_kind=kind,
                tokens=tokens,
                archived=archived,
                updated_at=_NOW,
            )
        }
    )


# ============================== §6: у каждого канала СВОЯ пара ==============================
def test_the_three_channel_fallbacks_are_distinct_in_the_fixture() -> None:
    """Предусловие всех кейсов ниже: на совпадающих числах подмена фолбэка НЕВИДИМА."""
    settings = _settings()

    assert (
        len(
            {
                settings.cloudpayments_subscription_tokens_grant,
                settings.adapty_subscription_tokens_grant,
                settings.subscription_credits_per_period,
            }
        )
        == 3
    )


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        (CHANNEL_CLOUDPAYMENTS, _CP_FALLBACK),
        (CHANNEL_ADAPTY, _ADAPTY_FALLBACK),
        (CHANNEL_MANUAL, _PERIOD_FALLBACK),
        (CHANNEL_STOREKIT, _PERIOD_FALLBACK),
    ],
)
def test_an_unmapped_product_falls_back_to_the_fallback_of_its_own_channel(
    channel: str, expected: int
) -> None:
    """Свести четыре пары к трём «по смыслу» запрещено: экономия строки стоит неверного начисления.

    Кейс обязан падать при подмене одного фолбэка другим — числа в фикстуре различны.
    """
    assert (
        subscription_credits(_UNMAPPED, channel, settings=_settings(), snapshot=EMPTY_SNAPSHOT)
        == expected
    )


@pytest.mark.parametrize(
    ("channel", "product_id", "expected"),
    [
        (CHANNEL_CLOUDPAYMENTS, _CLOUDPAYMENTS_ID, _CP_MAPPED),
        (CHANNEL_ADAPTY, _ADAPTY_ID, _ADAPTY_MAPPED),
        # Ручная выдача плана читает КАРТУ CloudPayments…
        (CHANNEL_MANUAL, _CLOUDPAYMENTS_ID, _CP_MAPPED),
    ],
)
def test_a_mapped_product_is_granted_by_the_map_of_its_own_channel(
    channel: str, product_id: str, expected: int
) -> None:
    assert (
        subscription_credits(product_id, channel, settings=_settings(), snapshot=EMPTY_SNAPSHOT)
        == expected
    )


def test_manual_grant_of_a_token_product_uses_the_period_fallback_not_the_cloudpayments_one() -> (
    None
):
    """§6: пара «карта + фолбэк» у ручной выдачи НЕ совпадает ни с одним вебхуком.

    Форма принимает продукты из `TOKEN_PRODUCTS`/`PRODUCTS_CATALOG`, которых в карте CloudPayments
    нет, — значит именно они и уходят в фолбэк. Подмена переменной изменила бы выданное число
    кредитов на инстансе, где эти две величины откалиброваны по-разному.
    """
    settings = _settings()

    granted = subscription_credits(
        _ONE_TIME_ID, CHANNEL_MANUAL, settings=settings, snapshot=EMPTY_SNAPSHOT
    )

    assert granted == _PERIOD_FALLBACK
    assert granted != _CP_FALLBACK  # diff: подмена фолбэка была бы видна


def test_storekit_subscription_knows_no_product_and_keeps_the_fixed_grant() -> None:
    settings = _settings()

    assert (
        subscription_credits(None, CHANNEL_STOREKIT, settings=settings, snapshot=EMPTY_SNAPSHOT)
        == _PERIOD_FALLBACK
    )
    assert (
        subscription_credits("", CHANNEL_STOREKIT, settings=settings, snapshot=EMPTY_SNAPSHOT)
        == _PERIOD_FALLBACK
    )


@pytest.mark.parametrize(
    "channel", [CHANNEL_CLOUDPAYMENTS, CHANNEL_ADAPTY, CHANNEL_MANUAL, CHANNEL_STOREKIT]
)
def test_a_subscription_overlay_wins_over_every_channel_map_and_fallback(channel: str) -> None:
    snapshot = _overlay(_UNMAPPED, tokens=9001)

    assert subscription_credits(_UNMAPPED, channel, settings=_settings(), snapshot=snapshot) == 9001


def test_a_one_time_overlay_never_grants_on_a_subscription_path() -> None:
    """Класс продукта проверяется ЯВНО: совпадение идентификатора начислением не является."""
    snapshot = _overlay(_UNMAPPED, tokens=9001, kind=PURCHASE_KIND_ONE_TIME)

    assert (
        subscription_credits(_UNMAPPED, CHANNEL_ADAPTY, settings=_settings(), snapshot=snapshot)
        == _ADAPTY_FALLBACK
    )


# ============================== разовая покупка =============================================
def test_one_time_credits_read_token_products_then_the_overlay() -> None:
    settings = _settings()

    assert one_time_credits(_ONE_TIME_ID, settings=settings, snapshot=EMPTY_SNAPSHOT) == 100
    assert (
        one_time_credits(
            _ONE_TIME_ID,
            settings=settings,
            snapshot=_overlay(_ONE_TIME_ID, tokens=250, kind=PURCHASE_KIND_ONE_TIME),
        )
        == 250
    )


def test_an_unknown_one_time_product_stays_unknown_and_is_not_invented() -> None:
    """`None` = «продукт инстансу неизвестен» — штатный отказ пути (`422` у StoreKit)."""
    assert (
        one_time_credits("no.such.product", settings=_settings(), snapshot=EMPTY_SNAPSHOT) is None
    )


def test_a_subscription_overlay_never_grants_on_the_one_time_path() -> None:
    snapshot = _overlay(_ONE_TIME_ID, tokens=9001, kind=PURCHASE_KIND_SUBSCRIPTION)

    assert one_time_credits(_ONE_TIME_ID, settings=_settings(), snapshot=snapshot) == 100


# ============================== каталог из пяти источников ==================================
def test_adapty_products_appear_in_the_catalog_for_the_first_time() -> None:
    """Аддитивное расширение, названное явно: прежде инстанс по ним начислял, а в CRM их не было.

    Каталог обязан показывать всё, за что начисляются кредиты, иначе оператор правит не тот
    продукт или не находит его вовсе. Строк больше; ни одно начисление от этого не меняется.
    """
    rows = {
        row.product_id: row for row in catalog_rows(settings=_settings(), snapshot=EMPTY_SNAPSHOT)
    }

    assert rows[_ADAPTY_ID].source == SOURCE_ADAPTY
    assert rows[_ADAPTY_ID].purchase_kind == PURCHASE_KIND_SUBSCRIPTION
    assert rows[_ADAPTY_ID].tokens == _ADAPTY_MAPPED
    assert rows[_ONE_TIME_ID].source == SOURCE_TOKEN_PRODUCTS
    assert rows[_CLOUDPAYMENTS_ID].source == SOURCE_CLOUDPAYMENTS
    assert rows[_CATALOG_ONLY_ID].source == SOURCE_PRODUCTS_CATALOG


def test_the_first_source_wins_on_an_identifier_collision() -> None:
    """Ключ оверлея — только `product_id`, поэтому порядок слияния фиксирован и назван."""
    settings = _settings(CLOUDPAYMENTS_PRODUCT_TOKENS=f'{{"{_ONE_TIME_ID}": {_CP_MAPPED}}}')

    rows = [
        row
        for row in catalog_rows(settings=settings, snapshot=EMPTY_SNAPSHOT)
        if row.product_id == _ONE_TIME_ID
    ]

    assert len(rows) == 1
    assert rows[0].source == SOURCE_TOKEN_PRODUCTS


def test_an_overlay_covers_an_env_row_without_changing_its_source() -> None:
    snapshot = _overlay(_CLOUDPAYMENTS_ID, tokens=777, name="Переименован")

    row = find_product(_CLOUDPAYMENTS_ID, settings=_settings(), snapshot=snapshot)

    assert row is not None
    assert row.tokens == 777
    assert row.name == "Переименован"
    assert row.updated_at == _NOW
    assert row.source == SOURCE_CLOUDPAYMENTS  # источник остался прежним


def test_an_operator_created_product_appears_as_its_own_catalog_row() -> None:
    snapshot = _overlay("operator.new", tokens=500, kind=PURCHASE_KIND_ONE_TIME, name="Новый")

    row = find_product("operator.new", settings=_settings(), snapshot=snapshot)

    assert row is not None
    assert row.source == SOURCE_OPERATOR
    assert row.tokens == 500
    assert row.purchase_kind == PURCHASE_KIND_ONE_TIME


def test_a_catalog_row_without_a_kind_or_credits_carries_none_rather_than_a_guess() -> None:
    """Материализовать недостающее догадкой ЗАПРЕЩЕНО: оверлей подписки с `tokens=0` обнулил бы
    грант канала, то есть правка, не касавшаяся начисления, изменила бы начисление."""
    settings = _settings(PRODUCTS_CATALOG='[{"productId": "bare.row", "title": "Без класса"}]')

    row = find_product("bare.row", settings=settings, snapshot=EMPTY_SNAPSHOT)

    assert row is not None
    assert row.purchase_kind is None
    assert row.tokens is None


def test_a_catalog_row_kind_tokens_maps_to_the_one_time_class() -> None:
    settings = _settings(
        PRODUCTS_CATALOG=(
            '[{"productId": "pack.row", "title": "Пакет", "kind": "tokens", "credits": 40}]'
        )
    )

    row = find_product("pack.row", settings=settings, snapshot=EMPTY_SNAPSHOT)

    assert row is not None
    assert row.purchase_kind == PURCHASE_KIND_ONE_TIME
    assert row.tokens == 40


# ============================== архив: адресат правила — КЛИЕНТ =============================
def test_archived_is_a_property_of_the_product_and_does_not_touch_granting() -> None:
    """«Перестал выдаваться» относится к КЛИЕНТУ приложения.

    Вебхук оплаты и оператор, выдающий план вручную, выполняют законные операции, и копировать
    на них правило витрины «по аналогии» запрещено — иначе архив ломал бы уже оплаченное.
    """
    settings = _settings()
    snapshot = _overlay(_CLOUDPAYMENTS_ID, tokens=888, archived=True)

    assert is_archived(_CLOUDPAYMENTS_ID, snapshot=snapshot) is True
    assert (
        subscription_credits(
            _CLOUDPAYMENTS_ID, CHANNEL_CLOUDPAYMENTS, settings=settings, snapshot=snapshot
        )
        == 888
    )
    assert _CLOUDPAYMENTS_ID in known_product_ids(settings=settings, snapshot=snapshot)
    row = find_product(_CLOUDPAYMENTS_ID, settings=settings, snapshot=snapshot)
    assert row is not None and row.archived is True


def test_an_archived_operator_product_leaves_the_shop_window_but_stays_in_the_catalog() -> None:
    settings = _settings()
    live = _overlay("operator.live", tokens=10, kind=PURCHASE_KIND_ONE_TIME)
    gone = _overlay("operator.gone", tokens=10, kind=PURCHASE_KIND_ONE_TIME, archived=True)

    assert [row.product_id for row in operator_created_rows(settings=settings, snapshot=live)] == [
        "operator.live"
    ]
    assert operator_created_rows(settings=settings, snapshot=gone) == []
    assert "operator.gone" in known_product_ids(settings=settings, snapshot=gone)


def test_known_product_ids_covers_the_whole_union_including_created_and_archived() -> None:
    """Множество, принимаемое ручной выдачей плана: расширение односторонне безопасно."""
    settings = _settings()
    snapshot = _overlay("operator.archived", tokens=10, archived=True)

    ids = known_product_ids(settings=settings, snapshot=snapshot)

    assert {
        _ONE_TIME_ID,
        _ADAPTY_ID,
        _CLOUDPAYMENTS_ID,
        _CATALOG_ONLY_ID,
        "operator.archived",
    } <= ids


def test_is_archived_is_false_for_a_product_without_an_overlay() -> None:
    assert is_archived(_ONE_TIME_ID, snapshot=EMPTY_SNAPSHOT) is False
    assert is_archived("никогда-не-существовал", snapshot=EMPTY_SNAPSHOT) is False


def test_find_product_returns_none_for_an_unknown_identifier() -> None:
    assert find_product("no.such", settings=_settings(), snapshot=EMPTY_SNAPSHOT) is None
