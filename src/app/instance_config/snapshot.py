"""Снимок операторских оверлеев в памяти процесса (ADR-099 §2).

**Почему снимок, а не чтение из БД на каждом обращении.** Значения читаются в том числе из
СИНХРОННЫХ, ЧИСТЫХ функций (сборка системного промта, резолв каталога моделей, гейт семейств
инструментов). Превратить их в ``async``-обращения к БД значило бы протащить сессию через весь
слой домена ради величины, меняющейся раз в месяц. Снимок — обычный неизменяемый словарь: любой
синхронный читатель берёт его без ``await`` и без сессии.

**Пустой снимок = сегодняшний день бит-в-бит.** Это дефолтное состояние модуля, а не результат
удачной загрузки: процесс, который ещё ничего не прочитал (или читает в тесте без БД), резолвит
каждую величину из env и кода — ровно как до выката.

**Отказ обновления НЕ откатывает цены.** Снимок остаётся прежним, пишется метрика и событие.
Возврат к env-дефолтам после того, как снимок уже был получен, — тихая отмена операторской
правки, и он запрещён.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import AdminProduct, AdminSetting, AdminTariff
from app.observability.logging import log_event
from app.observability.metrics import (
    admin_overrides_active,
    admin_overrides_refresh_failures_total,
    admin_overrides_snapshot_age_seconds,
)

logger = logging.getLogger("app.instance_config")

SCOPE_PRODUCTS = "products"
SCOPE_TARIFFS = "tariffs"
SCOPE_SETTINGS = "settings"


@dataclass(frozen=True)
class ProductOverlay:
    """Строка ``admin_products``: операторский оверлей продукта каталога.

    ⚠️ **Три значимых поля необязательны, и ``None`` здесь — не «нет данных», а высказывание**
    «оверлей этого поля не задаёт» (ADR-099 §6.1). Читатель обязан провалиться к ИСТОЧНИКУ
    (env-карта или ``PRODUCTS_CATALOG``), а не вернуть пустоту: иначе archived-правка строки,
    не касавшаяся начисления, обнулила бы грант. Слияние — ПОФИЛДОВОЕ у каждого читателя;
    заменить его одной проверкой «строка оверлея есть» нельзя — она вернула бы ``None`` там,
    где источник знает значение.
    """

    product_id: str
    name: str | None
    purchase_kind: str | None
    tokens: int | None
    archived: bool
    updated_at: datetime


@dataclass(frozen=True)
class TariffOverlay:
    """Строка ``admin_tariffs``: цена одного варианта генерации."""

    tariff_id: str
    tokens: int
    updated_at: datetime


@dataclass(frozen=True)
class SettingOverlay:
    """Строка ``admin_settings`` с УЖЕ приведённым к объявленному типу значением."""

    setting_id: str
    value: Any
    updated_at: datetime


@dataclass(frozen=True)
class InstanceConfigSnapshot:
    """Неизменяемый снимок трёх таблиц-оверлеев."""

    products: Mapping[str, ProductOverlay] = field(default_factory=dict)
    tariffs: Mapping[str, TariffOverlay] = field(default_factory=dict)
    settings: Mapping[str, SettingOverlay] = field(default_factory=dict)
    loaded_at: float | None = None

    def composition(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Состав оверлеев: идентификаторы по трём областям, в стабильном порядке."""
        return (
            tuple(sorted(self.products)),
            tuple(sorted(self.tariffs)),
            tuple(sorted(self.settings)),
        )


EMPTY_SNAPSHOT = InstanceConfigSnapshot()

_current: InstanceConfigSnapshot = EMPTY_SNAPSHOT

# Точка отсчёта возраста, пока снимок ни разу не загрузился (см. `_snapshot_age_seconds`).
_process_started_at = time.time()


def get_snapshot() -> InstanceConfigSnapshot:
    """Текущий снимок процесса. Никогда не ``None``: до первой загрузки — пустой."""
    return _current


def _snapshot_age_seconds() -> float:
    """Возраст снимка в секундах, вычисляемый В МОМЕНТ СКРЕЙПА.

    Скрейп-время, а не запись при обновлении: если фоновый обновитель умер, записанное при
    последнем успешном обновлении значение застыло бы, и алерт «правки оператора не
    применяются» не сработал бы никогда.

    ⚠️ **Пока НИ ОДНОЙ успешной загрузки не было, возраст считается от старта процесса, а не
    равен нулю.** Инстанс, у которого упала и стартовая загрузка, и все последующие, находится
    ровно в том состоянии, ради которого серия заведена — правки оператора не применяются, —
    и нулём объявил бы себя здоровым. Ноль здесь был бы не измерением, а молчанием.
    """
    loaded_at = _current.loaded_at
    reference = _process_started_at if loaded_at is None else loaded_at
    return max(0.0, time.time() - reference)


admin_overrides_snapshot_age_seconds.set_function(_snapshot_age_seconds)


def install_snapshot(snapshot: InstanceConfigSnapshot) -> None:
    """Заменить снимок процесса и обновить наблюдаемость состава."""
    global _current
    previous = _current
    _current = snapshot
    admin_overrides_active.labels(scope=SCOPE_PRODUCTS).set(len(snapshot.products))
    admin_overrides_active.labels(scope=SCOPE_TARIFFS).set(len(snapshot.tariffs))
    admin_overrides_active.labels(scope=SCOPE_SETTINGS).set(len(snapshot.settings))
    if previous.composition() != snapshot.composition():
        # Состав логируется на ИЗМЕНЕНИИ, а не на каждом тике окна: тик раз в 30 секунд на 41
        # инстансе — это поток строк, в котором настоящее изменение состава уже не видно.
        # Обесценивание канала стоит ровно столько же, сколько молчание.
        products, tariffs, settings_ids = snapshot.composition()
        log_event(
            logger,
            logging.INFO,
            "admin_overrides_snapshot_changed",
            products=list(products),
            tariffs=list(tariffs),
            settings=list(settings_ids),
        )


def reset_snapshot() -> None:
    """Вернуть процесс к пустому снимку (изоляция тестов и завершение lifespan)."""
    install_snapshot(EMPTY_SNAPSHOT)


async def load_snapshot(
    session: AsyncSession, settings: Settings | None = None
) -> InstanceConfigSnapshot:
    """Прочитать три таблицы целиком и собрать снимок. Ошибки БД НЕ перехватываются здесь."""
    from app.instance_config.settings_registry import (
        SettingValueError,
        find_setting,
        validate_setting_value,
    )

    cfg = settings or get_settings()
    products = {
        row.product_id: ProductOverlay(
            product_id=row.product_id,
            name=row.name,
            purchase_kind=row.purchase_kind,
            tokens=row.tokens,
            archived=row.archived,
            updated_at=row.updated_at,
        )
        for row in (await session.scalars(select(AdminProduct))).all()
    }
    tariffs = {
        row.tariff_id: TariffOverlay(
            tariff_id=row.tariff_id, tokens=row.tokens, updated_at=row.updated_at
        )
        for row in (await session.scalars(select(AdminTariff))).all()
    }
    resolved_settings: dict[str, SettingOverlay] = {}
    for row in (await session.scalars(select(AdminSetting))).all():
        # Строка игнорируется во ВСЕХ исходах ниже — оверлей не имеет права уронить инстанс
        # (§9). Различается только ЛЕЙБЛ: `reason` берётся из того же закрытого перечня, что и
        # у отказов правки (§10.0), и обязан следовать из наблюдаемого факта СВОЕЙ ветки.
        # Прежде все три исхода писались `type_mismatch`, и для сироты это было ложью: тип у
        # неё в порядке, применить её НЕКОМУ — дежурный искал кривое значение вместо строки,
        # потерявшей владельца, а настоящий рассинхрон типов тонул среди сирот.
        spec = find_setting(row.setting_id, cfg)
        if spec is None:
            # Настройка снята с этого инстанса: несоответствие лежит в РЕЕСТРЕ, а не в
            # значении. Виновник здесь — мы сами (в отличие от `unknown_id` на пути `PATCH`,
            # где идентификатор прислала CRM), но виновник в лейбл не кодируется: его называет
            # событие, в котором лейбл стоит.
            _log_ignored_setting(row.setting_id, "unknown_id")
            continue
        try:
            value = validate_setting_value(spec, row.value, cfg)
        except SettingValueError as exc:
            # Тот же признак, что и в `patch_setting` (`economics_service.py`): нарушен
            # объявленный ключ `constraints` (размерная граница) — `out_of_range`; нарушен тип
            # или перечень `options` — `type_mismatch`. Второго признака об одном факте не
            # заводим.
            _log_ignored_setting(
                row.setting_id, "out_of_range" if exc.constraint is not None else "type_mismatch"
            )
            continue
        resolved_settings[row.setting_id] = SettingOverlay(
            setting_id=row.setting_id, value=value, updated_at=row.updated_at
        )
    return InstanceConfigSnapshot(
        products=products,
        tariffs=tariffs,
        settings=resolved_settings,
        loaded_at=time.time(),
    )


def _log_ignored_setting(setting_id: str, reason: str) -> None:
    """Строка ``admin_settings`` не применена. ``reason`` — из перечня §10.0, не из своего.

    Собственный словарь об одном и том же факте («где лежит несоответствие») разошёлся бы с
    перечнем отказов правки, поэтому значения здесь — подмножество того же перечня: достижимы
    ``unknown_id`` / ``out_of_range`` / ``type_mismatch``, а остальные четыре недостижимы по
    построению (``conflict`` и ``unsupported_field`` требуют запроса, которого при чтении
    снимка нет; ``source_*`` — источника элемента, которого у настройки нет).
    """
    log_event(
        logger,
        logging.WARNING,
        "admin_override_value_ignored",
        setting_id=setting_id,
        reason=reason,
    )


async def refresh_snapshot(session: AsyncSession, settings: Settings | None = None) -> bool:
    """Обновить снимок процесса. ``False`` = отказ, прежний снимок СОХРАНЁН."""
    try:
        snapshot = await load_snapshot(session, settings)
    except ProgrammingError as exc:
        # Таблиц ещё нет (миграция не применена) либо форма разошлась с моделью.
        _record_refresh_failure("schema_mismatch", exc)
        return False
    except SQLAlchemyError as exc:
        _record_refresh_failure("db_error", exc)
        return False
    install_snapshot(snapshot)
    return True


def _record_refresh_failure(reason: str, exc: BaseException) -> None:
    admin_overrides_refresh_failures_total.labels(reason=reason).inc()
    log_event(
        logger,
        logging.ERROR,
        "admin_overrides_refresh_failed",
        reason=reason,
        error=str(exc),
    )


async def refresh_snapshot_from_pool(settings: Settings | None = None) -> bool:
    """Обновить снимок собственной сессией из пула (lifespan и фоновая задача)."""
    from app.db import get_sessionmaker

    try:
        maker = get_sessionmaker()
    except SQLAlchemyError as exc:  # pragma: no cover — движок не собрался
        _record_refresh_failure("db_error", exc)
        return False
    async with maker() as session:
        ok = await refresh_snapshot(session, settings)
        # Чтение не оставляет открытой транзакции на всё окно.
        await session.rollback()
        return ok


async def overrides_refresh_loop(stop: asyncio.Event, settings: Settings) -> None:
    """Фоновое обновление снимка раз в ``ADMIN_OVERRIDES_REFRESH_SECONDS``.

    Тот же приём, что у media-реконсилятора: интервал, событие остановки и полное подавление
    исключений внутри итерации — умерший обновитель не должен ронять процесс, но обязан быть
    ВИДЕН: возраст снимка растёт, и алерт по ``admin_overrides_snapshot_age_seconds`` сработает.
    """
    interval = settings.admin_overrides_refresh_window()
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            return
        try:
            await refresh_snapshot_from_pool(settings)
        except Exception as exc:  # noqa: BLE001 — цикл переживает любую ошибку итерации
            # `unexpected`, а НЕ `db_error`: `refresh_snapshot` обрабатывает `ProgrammingError`
            # и `SQLAlchemyError` ВНУТРИ себя и наружу их не выпускает, поэтому сюда доходит
            # ровно и только то, что ошибкой БД не является, — дефект нашего кода. Лейбл
            # `db_error` был бы здесь ложен ВСЕГДА и посылал бы дежурного чинить базу.
            # Контраст помечен с обеих сторон: ветки внутри `refresh_snapshot` и
            # `refresh_snapshot_from_pool`, где отказ ДЕЙСТВИТЕЛЬНО `SQLAlchemyError`,
            # остаются `db_error` — одну не переписывать «по аналогии» с другой.
            _record_refresh_failure("unexpected", exc)
