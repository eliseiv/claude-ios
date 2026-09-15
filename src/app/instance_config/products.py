"""Каталог продуктов инстанса и разрешение начислений (ADR-099 §6).

Каталог собран из ПЯТИ источников; оверлей ``admin_products`` накрывает их все по ключу
``product_id``.

⚠️ **Ключ оверлея — только ``product_id``, без канала.** Если один и тот же идентификатор
присутствует в двух картах, строка оверлея накрывает обе, а ``purchase_kind`` строки решает,
какой класс резолвера её увидит. Пара «продукт + канал» ключом не делается намеренно:
``product_id`` принадлежит стору, и в контракте CRM канала нет вовсе — составной ключ был бы
невыразим ни в списке, ни в пути `PATCH`. Коллизия карт — конфигурационная аномалия инстанса, и
лечится она разведением карт, а не вторым измерением ключа.

⚠️ **Каналов подписки ЧЕТЫРЕ, и у каждого своя пара «карта → фолбэк».** Фолбэки заданы РАЗНЫМИ
переменными и калибруются независимо; совпадение сегодняшних дефолтов ничего не гарантирует на
проде. Свести четыре пары к трём «по смыслу» запрещено: экономия одной строки здесь стоит
неверного начисления.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from app.config import Settings, get_settings
from app.instance_config.snapshot import InstanceConfigSnapshot, get_snapshot

PURCHASE_KIND_ONE_TIME = "one_time"
PURCHASE_KIND_SUBSCRIPTION = "subscription"
PURCHASE_KINDS = (PURCHASE_KIND_SUBSCRIPTION, PURCHASE_KIND_ONE_TIME)

# Каналы начисления по подписке. Имена — наши, наружу не уходят; они существуют, чтобы пара
# «карта продукта → фолбэк» выбиралась явно, а не «по смыслу».
CHANNEL_CLOUDPAYMENTS = "cloudpayments"
CHANNEL_ADAPTY = "adapty"
CHANNEL_MANUAL = "manual"
CHANNEL_STOREKIT = "storekit"

# ADR-106 §D1: источник суммы периода подписки. Порядок и суммы ADR-099 §6 не меняются.
CREDITS_SOURCE_OVERLAY = "overlay"
CREDITS_SOURCE_CHANNEL_MAP = "channel_map"
CREDITS_SOURCE_CHANNEL_FALLBACK = "channel_fallback"

SOURCE_TOKEN_PRODUCTS = "token_products"
SOURCE_ADAPTY = "adapty_product_tokens"
SOURCE_CLOUDPAYMENTS = "cloudpayments_product_tokens"
SOURCE_PRODUCTS_CATALOG = "products_catalog"
SOURCE_OPERATOR = "admin_products"


@dataclass(frozen=True)
class ProductRow:
    """Одна строка каталога продуктов инстанса."""

    product_id: str
    name: str
    tokens: int | None
    purchase_kind: str | None
    archived: bool
    updated_at: datetime | None
    source: str


def _catalog_entry_id(entry: dict[str, Any]) -> str | None:
    pid = entry.get("productId") or entry.get("product_id")
    return pid if isinstance(pid, str) and pid else None


def _catalog_entry_kind(entry: dict[str, Any]) -> str | None:
    kind = entry.get("kind")
    if kind == PURCHASE_KIND_SUBSCRIPTION:
        return PURCHASE_KIND_SUBSCRIPTION
    if kind == "tokens":
        return PURCHASE_KIND_ONE_TIME
    return None


def _catalog_entry_credits(entry: dict[str, Any]) -> int | None:
    credits = entry.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, int):
        return None
    return credits


def catalog_rows(
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> list[ProductRow]:
    """Слияние пяти источников; порядок фиксирован, первое вхождение выигрывает.

    ``ADAPTY_PRODUCT_TOKENS`` попадает в каталог ВПЕРВЫЕ: прежде инстанс по этим продуктам
    начислял, а в CRM они видны не были — оператор правил не тот продукт или не находил его
    вовсе. Строк в ответе становится больше; ни одно начисление от этого не меняется.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    rows: list[ProductRow] = []
    seen: set[str] = set()

    def add(product_id: str, name: str, tokens: int | None, kind: str | None, source: str) -> None:
        if product_id in seen:
            return
        seen.add(product_id)
        rows.append(
            _apply_overlay(
                ProductRow(
                    product_id=product_id,
                    name=name,
                    tokens=tokens,
                    purchase_kind=kind,
                    archived=False,
                    updated_at=None,
                    source=source,
                ),
                snap,
            )
        )

    for product_id, credits in cfg.token_products().items():
        add(product_id, f"{credits} tokens", credits, PURCHASE_KIND_ONE_TIME, SOURCE_TOKEN_PRODUCTS)
    for product_id, credits in cfg.adapty_product_tokens().items():
        add(product_id, product_id, credits, PURCHASE_KIND_SUBSCRIPTION, SOURCE_ADAPTY)
    for product_id, credits in cfg.cloudpayments_product_tokens().items():
        add(product_id, product_id, credits, PURCHASE_KIND_SUBSCRIPTION, SOURCE_CLOUDPAYMENTS)
    for entry in cfg.products_catalog():
        catalog_product_id = _catalog_entry_id(entry)
        if catalog_product_id is None:
            continue
        display_name = entry.get("title") or entry.get("name") or catalog_product_id
        add(
            catalog_product_id,
            str(display_name),
            _catalog_entry_credits(entry),
            _catalog_entry_kind(entry),
            SOURCE_PRODUCTS_CATALOG,
        )
    for overlay in snap.products.values():
        if overlay.product_id in seen:
            continue
        seen.add(overlay.product_id)
        rows.append(
            ProductRow(
                product_id=overlay.product_id,
                # Источника у строки нет вовсе, поэтому проваливаться некуда: при незаданном
                # названии показываем идентификатор. `tokens`/`purchase_kind` остаются пустыми —
                # подставить сюда нечего, и подстановка была бы догадкой (ADR-099 §6.1).
                name=overlay.name if overlay.name is not None else overlay.product_id,
                tokens=overlay.tokens,
                purchase_kind=overlay.purchase_kind,
                archived=overlay.archived,
                updated_at=overlay.updated_at,
                source=SOURCE_OPERATOR,
            )
        )
    return rows


def _apply_overlay(row: ProductRow, snapshot: InstanceConfigSnapshot) -> ProductRow:
    """Слияние строки источника с оверлеем — ПОФИЛДОВОЕ (ADR-099 §6.1).

    Значение оверлея побеждает ТОЛЬКО там, где оно задано; ``None`` означает «оверлей этого
    поля не задаёт» ⇒ остаётся значение источника. Безусловная подстановка всех полей затёрла
    бы пустотой живые значения источника у строки, созданной правкой одного ``archived``.
    """
    overlay = snapshot.products.get(row.product_id)
    if overlay is None:
        return row
    return ProductRow(
        product_id=row.product_id,
        name=overlay.name if overlay.name is not None else row.name,
        tokens=overlay.tokens if overlay.tokens is not None else row.tokens,
        purchase_kind=(
            overlay.purchase_kind if overlay.purchase_kind is not None else row.purchase_kind
        ),
        archived=overlay.archived,
        updated_at=overlay.updated_at,
        source=row.source,
    )


def find_product(
    product_id: str,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> ProductRow | None:
    """Строка каталога по идентификатору, либо ``None`` — продукта на инстансе нет."""
    for row in catalog_rows(settings=settings, snapshot=snapshot):
        if row.product_id == product_id:
            return row
    return None


def known_product_ids(
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> set[str]:
    """Весь объединённый каталог, ВКЛЮЧАЯ созданные оператором и архивные.

    Архивный продукт остаётся допустимым для ручной выдачи плана: «перестал выдаваться»
    относится к клиенту приложения, а оператор выполняет законную операцию.
    """
    return {row.product_id for row in catalog_rows(settings=settings, snapshot=snapshot)}


# --- Начисления --------------------------------------------------------------------------


def one_time_credits(
    product_id: str,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> int | None:
    """Кредиты за разовую покупку. ``None`` = продукт инстансу неизвестен (штатный отказ пути).

    Класс продукта проверяется явно: оверлей подписки не имеет права начислить по разовой
    покупке, даже если идентификатор совпал.

    ⚠️ **Условие «И ``tokens`` задан» — не защита от ``None``, а сама семантика ADR-099 §6.1.**
    Строка оверлея с пустым числом означает «оверлей числа не задаёт» ⇒ читатель обязан
    провалиться к источнику. Иначе archived-правка, начисления не касавшаяся, превратила бы
    продукт в «неизвестный» — ровно то изменение начисления правкой, его не касавшейся,
    которое запрещено несущим свойством волны.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    overlay = snap.products.get(product_id)
    if (
        overlay is not None
        and overlay.purchase_kind == PURCHASE_KIND_ONE_TIME
        and overlay.tokens is not None
    ):
        return overlay.tokens
    return cfg.token_products().get(product_id)


def one_time_product_ids(
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> frozenset[str]:
    """Идентификаторы, по которым инстанс умеет начислить как за РАЗОВУЮ покупку.

    ⚠️ Существует ради формы «классификация по одному источнику, а начисление по другому».
    Место, которое решает «это разовая покупка или подписка», обязано смотреть в ТО ЖЕ
    множество, из которого потом берётся сумма: иначе созданный оператором продукт, не
    найденный в env-карте, переклассифицируется в подписку и начислится из фолбэка канала
    вместо своих ``tokens``.

    На пустом оверлее равно ключам ``TOKEN_PRODUCTS`` — сегодняшнее поведение бит-в-бит.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    ids = set(cfg.token_products())
    for overlay in snap.products.values():
        if overlay.purchase_kind == PURCHASE_KIND_ONE_TIME:
            ids.add(overlay.product_id)
        elif overlay.purchase_kind == PURCHASE_KIND_SUBSCRIPTION:
            # Оверлей подписки СНИМАЕТ продукт с разового класса: класс решает, какой резолвер
            # прочитает строку, и оставить идентификатор в обоих множествах значило бы
            # позволить одному продукту начислиться двумя разными правилами.
            ids.discard(overlay.product_id)
        # ⚠️ Класс НЕ ЗАДАН (`None`) — множество НЕ ТРОГАЕМ: оверлей о классе не высказывается
        # (ADR-099 §6.1). Снять продукт с разового множества здесь значило бы
        # переклассифицировать его правкой одного `archived` и начислить из фолбэка канала
        # вместо `TOKEN_PRODUCTS`.
    return frozenset(ids)


class SubscriptionCredits(NamedTuple):
    """Сумма периода подписки и её источник (ADR-106 §D1)."""

    amount: int
    source: str


def subscription_credits(
    product_id: str | None,
    channel: str,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> SubscriptionCredits:
    """Кредиты за период подписки: оверлей → карта КАНАЛА → фолбэк ЭТОГО ЖЕ канала.

    Пара «карта + фолбэк» выбирается каналом, а не «по смыслу»: у ручной выдачи плана карта
    CloudPayments, но фолбэк — ``SUBSCRIPTION_CREDITS_PER_PERIOD``, и подмена переменной
    изменила бы выданное число кредитов на инстансе, где эти величины откалиброваны раздельно.
    Вместе с суммой возвращается источник (ADR-106 §D1) — по нему вызывающий решает, заведён ли
    продукт (``is_product_unmapped``).
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    if product_id:
        overlay = snap.products.get(product_id)
        # «И `tokens` задан» — семантика §6.1, а не защита от `None`: незаданное число обязано
        # провалиться к карте канала и его фолбэку, иначе archived-правка обнулила бы грант.
        if (
            overlay is not None
            and overlay.purchase_kind == PURCHASE_KIND_SUBSCRIPTION
            and overlay.tokens is not None
        ):
            return SubscriptionCredits(overlay.tokens, CREDITS_SOURCE_OVERLAY)
    key = product_id or ""
    if channel == CHANNEL_CLOUDPAYMENTS:
        return _map_or_fallback(
            cfg.cloudpayments_product_tokens().get(key), cfg.cloudpayments_subscription_tokens_grant
        )
    if channel == CHANNEL_ADAPTY:
        return _map_or_fallback(
            cfg.adapty_product_tokens().get(key), cfg.adapty_subscription_tokens_grant
        )
    if channel == CHANNEL_MANUAL:
        return _map_or_fallback(
            cfg.cloudpayments_product_tokens().get(key), cfg.subscription_credits_per_period
        )
    return SubscriptionCredits(cfg.subscription_credits_per_period, CREDITS_SOURCE_CHANNEL_FALLBACK)


def _map_or_fallback(mapped: int | None, fallback: int) -> SubscriptionCredits:
    # `or`, а не `is None`: пустое (нулевое) значение карты и прежде проваливалось к фолбэку.
    if mapped:
        return SubscriptionCredits(mapped, CREDITS_SOURCE_CHANNEL_MAP)
    return SubscriptionCredits(fallback, CREDITS_SOURCE_CHANNEL_FALLBACK)


def is_product_unmapped(
    product_id: str | None,
    channel: str,
    credits: SubscriptionCredits,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> bool:
    """Предикат «продукт подписки не заведён» — функция пары (канал, источник), ADR-106 §D2.

    ``adapty``/``cloudpayments``: сумма из фолбэка канала. ``storekit``: фолбэк — штатная сумма
    канала без карты, поэтому сигнал только когда инстанс не знает продукт вовсе. ``manual``:
    никогда (продукт проверяется по каталогу до выдачи).
    """
    if credits.source != CREDITS_SOURCE_CHANNEL_FALLBACK:
        return False
    if channel in (CHANNEL_ADAPTY, CHANNEL_CLOUDPAYMENTS):
        return True
    if channel == CHANNEL_STOREKIT:
        return (
            not product_id or find_product(product_id, settings=settings, snapshot=snapshot) is None
        )
    return False


def is_archived(
    product_id: str,
    *,
    snapshot: InstanceConfigSnapshot | None = None,
) -> bool:
    """Снят ли продукт с ВИТРИНЫ. На начисления и на ручную выдачу плана не влияет."""
    snap = snapshot if snapshot is not None else get_snapshot()
    overlay = snap.products.get(product_id)
    return overlay is not None and overlay.archived


def operator_created_rows(
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> list[ProductRow]:
    """Продукты, которых нет ни в одном env-источнике, — их витриной владеем мы.

    ⚠️ **В витрину не попадает строка, которую нечем описать** (ADR-099 §6). Продукт, созданный
    ``POST``, всегда несёт и класс, и число (оба обязательны в теле). Но строка, созданная
    правкой одного ``archived``, несёт только флаг, и если её env-источник впоследствии исчез,
    она остаётся сиротой без имени, класса и числа. Показать такую позицию клиенту нельзя:
    у неё нет ни количества кредитов, ни класса покупки. Условие включения — «строка не имеет
    источника И несёт класс И несёт число», а не «строка не имеет источника».
    """
    return [
        row
        for row in catalog_rows(settings=settings, snapshot=snapshot)
        if row.source == SOURCE_OPERATOR
        and not row.archived
        and row.purchase_kind is not None
        and row.tokens is not None
    ]
