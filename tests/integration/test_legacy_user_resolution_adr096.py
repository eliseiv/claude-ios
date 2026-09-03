"""Разрешение платежа по идентификатору ПРЕЖНЕГО сервиса (ADR-096).

Перенос с 232 выдал людям новые внутренние идентификаторы, а поставщик продолжает присылать
старые: старое приложение живо, платежи создаются им, а вебхук настроен уже на новый сервис.
Разрешение знало два пути — `users.id` и `auth_devices.device_id` — и оба промахивались, поэтому
оплата отбрасывалась как `user_not_found`. Отказ молчаливый и денежный: провайдеру отвечают 200,
он больше не повторяет, деньги списаны, начисления нет, и следа не остаётся.

Проверяется не «третья ветвь есть», а порядок и границы: живые данные обязаны иметь приоритет над
исторической таблицей, а чужой идентификатор — по-прежнему не разрешаться ни во что.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.billing_common.resolve import (
    RESOLVED_VIA_DEVICE_ID,
    RESOLVED_VIA_LEGACY_ID,
    RESOLVED_VIA_USER_ID,
    resolve_user,
)
from tests.conftest import seed_user


@pytest.mark.asyncio
async def test_legacy_id_resolves_to_the_current_user(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж под старым идентификатором находит нынешнего владельца."""
    legacy = uuid.uuid4()
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await s.execute(
            text("INSERT INTO legacy_user_ids (legacy_user_id, user_id) VALUES (:l, :u)"),
            {"l": str(legacy), "u": str(uid)},
        )
        await s.commit()

        resolved = await resolve_user(s, legacy)
        assert resolved is not None, "старый идентификатор не разрешился — деньги потеряются"
        assert resolved == (uid, RESOLVED_VIA_LEGACY_ID)


@pytest.mark.asyncio
async def test_live_data_wins_over_the_historical_table(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Порядок ветвей нормативен: `users` и `auth_devices` важнее исторической таблицы.

    Историческая таблица заполняется разово из данных переноса и с тех пор не обновляется. Если бы
    она проверялась раньше живых, устаревшая строка увела бы ЧУЖОЙ платёж не тому человеку — а это
    деньги, и ошибка была бы не видна ни в одном ответе.
    """
    async with db_sessionmaker() as s:
        real = await seed_user(s)
        other = await seed_user(s)
        # Заведомо противоречивая строка: старый id совпадает с id ЖИВОГО пользователя.
        await s.execute(
            text("INSERT INTO legacy_user_ids (legacy_user_id, user_id) VALUES (:l, :u)"),
            {"l": str(real), "u": str(other)},
        )
        await s.commit()

        resolved = await resolve_user(s, real)
        assert resolved == (real, RESOLVED_VIA_USER_ID), "историческая строка перебила живого"


@pytest.mark.asyncio
async def test_device_still_wins_over_legacy(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Устройство — тоже живые данные, и оно тоже важнее исторической таблицы."""
    dev = uuid.uuid4()
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
        await s.execute(
            text("INSERT INTO auth_devices (device_id, user_id) VALUES (:d, :u)"),
            {"d": str(dev).upper(), "u": str(owner)},
        )
        await s.execute(
            text("INSERT INTO legacy_user_ids (legacy_user_id, user_id) VALUES (:l, :u)"),
            {"l": str(dev), "u": str(other)},
        )
        await s.commit()

        resolved = await resolve_user(s, dev)
        assert resolved == (owner, RESOLVED_VIA_DEVICE_ID)


@pytest.mark.asyncio
async def test_unknown_identifier_still_resolves_to_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Третья ветвь НЕ должна превращать неизвестный идентификатор в кого-нибудь.

    Обработчик вебхука публичен: разрешись чужой идентификатор хоть во что-то — и посторонняя
    оплата начислилась бы случайному человеку.
    """
    async with db_sessionmaker() as s:
        await seed_user(s)
        assert await resolve_user(s, uuid.uuid4()) is None
