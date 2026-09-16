"""JSON `null` в `chat_steps.usage`: чтение переживает его, запись больше не производит.

Прод-дефект: `GET /v1/admin/users/{id}` и `…/requests` отвечали `500`
(`AttributeError: 'NoneType' object has no attribute 'get'`). Механизм — в трёх звеньях, и здесь
закреплены два из них, третье (приведение накопленного) живёт в миграции `0034`:

1. **Запись.** Колонка объявлялась `mapped_column(JSONB, ...)`, а у SQLAlchemy по умолчанию
   `none_as_null=False`: питоновский `None` уезжает в базу скаляром `null`, а не SQL NULL.
   Assistant-шаги без счётчиков (пометка `turnFailed`, ответ медиа-визарда) писались именно так.
2. **Чтение.** JSON `null` — не SQL NULL, поэтому `s.usage IS NOT NULL` для него ИСТИННО, и
   агрегат CRM клал такой шаг в массив `usages`; в Python он приходит элементом `None`, и
   тарификация хода обращалась к нему как к словарю.

Норма для читателя — из докстринга самой функции: ход, у которого ХОТЬ ОДИН вызов неоценим,
стоит `None` целиком. Шаг без счётчиков неоценим по определению, поэтому частичная сумма
запрещена: она занижала бы себестоимость, выглядя полной.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import asyncpg as asyncpg_dialect

from app.models.tables import ChatStep
from app.observability.metrics import chat_unpriced_steps_total
from app.pricing import provider_prices
from app.pricing.provider_prices import (
    PROVIDER_OPENAI,
    chat_cost_usd,
    chat_cost_usd_by_provider,
    report_chat_step_pricing,
)


def _usage(model: str = "gpt-5.1") -> dict[str, Any]:
    return {"model": model, "inputTokens": 1000, "outputTokens": 200}


# Всё, чем JSONB-колонка может оказаться, не будучи словарём: `None` — наблюдаемый на проде
# случай (JSON `null`), остальные — та же форма дефекта, ничем на уровне БД не запрещённая.
_NOT_A_MAPPING: list[Any] = [None, "gpt-5.1", 42, 1.5, True, [], ["gpt-5.1"]]


# ============================== чтение: ход становится `None` ==============================


@pytest.mark.parametrize("junk", _NOT_A_MAPPING)
def test_turn_of_one_non_mapping_step_is_unpriceable_instead_of_raising(junk: Any) -> None:
    """До правки `None`-элемент давал `AttributeError` и `500` на ручке CRM."""
    assert chat_cost_usd_by_provider([junk]) is None
    assert chat_cost_usd([junk]) is None


@pytest.mark.parametrize("junk", _NOT_A_MAPPING)
def test_one_non_mapping_step_nulls_the_whole_turn_next_to_a_priced_call(junk: Any) -> None:
    """Частичная сумма запрещена нормой функции — и до, и после элемента с дефектом."""
    assert chat_cost_usd_by_provider([_usage(), junk]) is None
    assert chat_cost_usd_by_provider([junk, _usage()]) is None
    assert chat_cost_usd([_usage(), junk]) is None


def test_a_healthy_turn_is_still_priced() -> None:
    """Контроль: защита обязана отсекать только испорченный элемент, а не тарификацию вообще."""
    per_provider = chat_cost_usd_by_provider([_usage()])
    assert per_provider is not None
    assert set(per_provider) == {PROVIDER_OPENAI}
    assert per_provider[PROVIDER_OPENAI] > 0


# ===================== пишущий путь: неоценимый шаг остаётся слышен ========================


@pytest.fixture
def unpriced_counter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Сброс ПРОЦЕССНОГО состояния серии: счётчик и множество дедупа лога — глобальны."""
    chat_unpriced_steps_total.clear()
    monkeypatch.setattr(provider_prices, "_LOGGED_UNPRICED", set())
    monkeypatch.setattr(provider_prices, "_LOGGED_UNPRICED_CAP_ANNOUNCED", False)
    try:
        yield
    finally:
        chat_unpriced_steps_total.clear()


@pytest.mark.parametrize("junk", _NOT_A_MAPPING)
@pytest.mark.usefixtures("unpriced_counter")
def test_non_mapping_step_is_reported_as_no_model_and_does_not_raise(junk: Any) -> None:
    """`reason` — ОГРАНИЧЕННЫЙ enum (`docs/01-architecture.md`): четвёртого значения не заводим.

    Элемент-не-словарь неоценим по самой базовой причине: имени модели нет. Метка `model` —
    `none`, как у любого шага без имени, а не сырое значение из истории.
    """
    report_chat_step_pricing(junk)

    value = chat_unpriced_steps_total.labels(model="none", reason="no_model")._value.get()  # noqa: SLF001
    assert value == 1


# ================== запись: колонка больше не делает JSON `null` ==================


def test_usage_column_binds_python_none_to_sql_null() -> None:
    """Дом починки — САМА колонка, поэтому проверяется её тип, а не вызывающий его код.

    `bind_processor(...)(None) is None` означает «драйверу уходит SQL NULL»; строка `'null'`
    означала бы JSON-скаляр, то есть ровно прод-дефект.
    """
    dialect = asyncpg_dialect.dialect()
    processor = ChatStep.__table__.c.usage.type.bind_processor(dialect)

    assert processor is None or processor(None) is None


def test_the_check_above_is_not_vacuous_a_default_jsonb_would_store_json_null() -> None:
    """Положительный контроль: без `none_as_null=True` тот же путь даёт скаляр `null`.

    Без этого теста предыдущий прошёл бы и в мире, где SQLAlchemy вообще перестала кодировать
    `None`, — то есть доказывал бы не свойство колонки, а отсутствие обработчика.
    """
    dialect = asyncpg_dialect.dialect()
    processor = JSONB().bind_processor(dialect)

    assert processor is not None
    assert processor(None) == "null"
