"""Наблюдаемость непрайсуемого шага: `chat_unpriced_steps_total` + дедуплицированный лог (ADR-079).

Непрайсуемый шаг обнуляет себестоимость ВСЕГО хода, и оператор видит пустую ячейку
«Себестоимость» — неотличимо от «на этом инстансе не было трафика». Больше ничто не шевелится:
вызов удался, кредит списан, upstream-ошибки нет. Молчание здесь и позволило дрейфу имени модели
идти незамеченным, поэтому серия — обязательная часть починки, а не украшение.

Что здесь закреплено:

- серия считает ШАГИ и живёт на ПИШУЩЕМ пути (`report_chat_step_pricing`). Путь ЧТЕНИЯ
  (`chat_cost_usd_by_provider`) тарифицирует ту же сохранённую историю на каждый рендер — и по два
  раза на карточку, — поэтому он обязан молчать: иначе серия считала бы рендеры, а до открытия CRM
  не сообщала бы вообще ничего;
- `reason` различает три разных дефекта: неизвестная модель, отсутствующее имя, отсутствующие
  счётчики токенов;
- лог дедуплицирован по `(model, reason)`: счётчик несёт ЧАСТОТУ, лог — ИМЯ, один раз;
- за кэпом лог МОЛЧИТ (одно событие-граница), а счётчик продолжает расти. Кэп, который перестаёт
  ЗАПОМИНАТЬ, но продолжает ПИСАТЬ, превратил бы свой худший случай в поток WARNING'ов.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from app.observability.metrics import chat_unpriced_steps_total
from app.pricing import provider_prices
from app.pricing.provider_prices import chat_cost_usd_by_provider, report_chat_step_pricing

_LOGGER_NAME = "app.pricing.provider_prices"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, name: str) -> list[dict[str, Any]]:
        """Поля события без `requestId` — его добавляет `log_event` всем событиям без разбора."""
        out: list[dict[str, Any]] = []
        for record in self.records:
            if record.getMessage() != name:
                continue
            fields = getattr(record, "extra_fields", None)
            fields = fields if isinstance(fields, dict) else {}
            out.append({k: v for k, v in fields.items() if k != "requestId"})
        return out


@pytest.fixture
def unpriced(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Capture]:
    """Полный сброс процессного состояния серии: счётчик, множество дедупа, флаг кэпа, логгер.

    Всё перечисленное — ГЛОБАЛЬНОЕ состояние процесса, и без сброса тест зависел бы от того, что
    оставили предыдущие (в том числе интеграционные, которые гоняют реальный ход чата). Логгер
    вдобавок принудительно включается: интеграционный `create_app()` вызывает `configure_logging`,
    которая чистит хендлеры root'а, и в паре с плагином логирования pytest'а это оставляет логгер
    `disabled=True` — записи молча терялись бы в зависимости от порядка тестов.
    """
    chat_unpriced_steps_total.clear()
    monkeypatch.setattr(provider_prices, "_LOGGED_UNPRICED", set())
    monkeypatch.setattr(provider_prices, "_LOGGED_UNPRICED_CAP_ANNOUNCED", False)

    logger = logging.getLogger(_LOGGER_NAME)
    handler = _Capture()
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled
        chat_unpriced_steps_total.clear()


def _count(model: str, reason: str) -> float:
    return chat_unpriced_steps_total.labels(model=model, reason=reason)._value.get()  # noqa: SLF001


def _total() -> float:
    return sum(
        sample.value
        for metric in chat_unpriced_steps_total.collect()
        for sample in metric.samples
        if sample.name == "chat_unpriced_steps_total"
    )


def _usage(model: Any = "gpt-5.1", *, counts: bool = True) -> dict[str, Any]:
    usage: dict[str, Any] = {"inputTokens": 100, "outputTokens": 20} if counts else {}
    if model is not None:
        usage["model"] = model
    return usage


# ================================ три причины ================================


def test_unknown_model_increments_once_with_reason_unknown_model(unpriced: _Capture) -> None:
    report_chat_step_pricing(_usage("gpt-9-unreleased"))

    assert _count("gpt-9-unreleased", "unknown_model") == 1
    assert _total() == 1
    assert unpriced.messages("chat_step_unpriced") == [
        {"model": "gpt-9-unreleased", "reason": "unknown_model"}
    ]


@pytest.mark.parametrize("missing", [None, "", 123, {"nested": "x"}])
def test_missing_model_name_is_reported_as_no_model(unpriced: _Capture, missing: Any) -> None:
    """Метка `model` ограничена: имени нет — метка `none`, а не сырое значение из истории."""
    report_chat_step_pricing(_usage(missing))

    assert _count("none", "no_model") == 1
    assert _total() == 1


def test_known_model_without_token_counts_is_reported_as_no_token_counts(
    unpriced: _Capture,
) -> None:
    """Шаг, записанный до персиста счётчиков: умножать не на что, и это ДРУГОЙ дефект."""
    report_chat_step_pricing(_usage("gpt-5.1", counts=False))

    assert _count("gpt-5.1", "no_token_counts") == 1
    assert _total() == 1


# ================================ прайсуемый шаг молчит ================================


@pytest.mark.parametrize(
    "model",
    ["gpt-5.1", "gpt-5.1-2025-11-13", "claude-sonnet-4-5", "claude-sonnet-4-5-20250929"],
)
def test_priceable_step_moves_nothing(unpriced: _Capture, model: str) -> None:
    """Серия — сигнал дефекта, а не счётчик трафика. Снапшот теперь тоже прайсуем."""
    report_chat_step_pricing(_usage(model))

    assert _total() == 0
    assert unpriced.messages("chat_step_unpriced") == []


def test_counter_counts_calls_not_turns(unpriced: _Capture) -> None:
    """Ход tool-loop’а — несколько вызовов LLM; каждый непрайсуемый вызов считается отдельно."""
    for _ in range(3):
        report_chat_step_pricing(_usage("gpt-9-unreleased"))

    assert _count("gpt-9-unreleased", "unknown_model") == 3


# ================================ путь чтения молчит ================================


def test_read_path_never_moves_the_counter(unpriced: _Capture) -> None:
    """`chat_cost_usd_by_provider` вызывается на каждый рендер и дважды на карточку.

    Перенос отчёта на пишущий путь — предмет этого теста: считай серия здесь, одна и та же
    сохранённая строка истории накручивала бы счётчик при каждом открытии CRM.
    """
    turn = [_usage("gpt-9-unreleased")]
    for _ in range(5):
        assert chat_cost_usd_by_provider(turn) is None

    assert _total() == 0
    assert unpriced.messages("chat_step_unpriced") == []


# ================================ дедуп лога ================================


def test_log_is_deduplicated_by_model_and_reason_while_the_counter_keeps_growing(
    unpriced: _Capture,
) -> None:
    for _ in range(10):
        report_chat_step_pricing(_usage("gpt-9-unreleased"))

    assert _count("gpt-9-unreleased", "unknown_model") == 10
    assert len(unpriced.messages("chat_step_unpriced")) == 1


def test_same_model_with_two_reasons_logs_twice(unpriced: _Capture) -> None:
    """Ключ дедупа — ПАРА: другая причина у той же модели описывает другой дефект."""
    report_chat_step_pricing(_usage("gpt-9-unreleased"))
    report_chat_step_pricing(_usage("gpt-5.1", counts=False))

    logged = unpriced.messages("chat_step_unpriced")
    assert {(entry["model"], entry["reason"]) for entry in logged} == {
        ("gpt-9-unreleased", "unknown_model"),
        ("gpt-5.1", "no_token_counts"),
    }


# ================================ кэп ================================


def test_past_the_cap_the_log_goes_silent_and_the_counter_keeps_reporting(
    unpriced: _Capture,
) -> None:
    cap = provider_prices._LOGGED_UNPRICED_CAP  # noqa: SLF001
    for i in range(cap):
        report_chat_step_pricing(_usage(f"unknown-model-{i}"))
    assert len(unpriced.messages("chat_step_unpriced")) == cap
    assert unpriced.messages("chat_step_unpriced_log_capped") == []

    # Первое имя ЗА кэпом: ровно одно событие-граница, самого имени в логе уже нет.
    report_chat_step_pricing(_usage("over-the-cap-1"))
    capped = unpriced.messages("chat_step_unpriced_log_capped")
    assert capped == [{"distinct_names": cap}]
    assert len(unpriced.messages("chat_step_unpriced")) == cap

    # Дальше лог молчит совсем — включая повтор самого события-границы.
    for i in range(2, 12):
        report_chat_step_pricing(_usage(f"over-the-cap-{i}"))
    assert unpriced.messages("chat_step_unpriced_log_capped") == [{"distinct_names": cap}]
    assert len(unpriced.messages("chat_step_unpriced")) == cap

    # А счётчик продолжает работать: за кэпом он единственный, кто сообщает о дефекте.
    assert _count("over-the-cap-1", "unknown_model") == 1
    assert _count("over-the-cap-11", "unknown_model") == 1
    assert _total() == cap + 11


def test_a_name_seen_before_the_cap_is_still_deduplicated_after_it(unpriced: _Capture) -> None:
    """Кэп не ломает дедуп уже известных имён: они не начинают писаться заново."""
    cap = provider_prices._LOGGED_UNPRICED_CAP  # noqa: SLF001
    for i in range(cap):
        report_chat_step_pricing(_usage(f"unknown-model-{i}"))
    report_chat_step_pricing(_usage("over-the-cap-1"))

    report_chat_step_pricing(_usage("unknown-model-0"))

    assert len(unpriced.messages("chat_step_unpriced")) == cap
    assert _count("unknown-model-0", "unknown_model") == 2
