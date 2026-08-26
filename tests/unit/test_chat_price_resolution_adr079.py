"""Резолв датированного снапшота модели к алиасу закупочной таблицы (ADR-079).

`CHAT_TOKEN_PRICES` ключуется АЛИАСОМ (`gpt-5.1`), а провайдер отвечает и в историю попадает
ДАТИРОВАННЫЙ СНАПШОТ (`gpt-5.1-2025-11-13`, `claude-sonnet-4-5-20250929`). Снапшот тарифицируется
провайдером по прайсу своего алиаса, поэтому отображение одного на другой ЧИТАЕТ цену, которая у
нас есть, а не выдумывает новую. Себестоимость чата нигде не хранится — она считается при чтении,
поэтому этот же резолв чинит и УЖЕ НАКОПЛЕННУЮ историю, без миграции.

Обратная сторона — доктрина модуля «цены, которой нет, не существует»: `None`, а не `0.0`.
Поэтому резолвится ТОЛЬКО суффикс-дата. `gpt-5-pro-…` и `…-chat-latest` — ДРУГИЕ модели со своим
прайсом; списать их по ставке базовой модели значило бы опубликовать выдуманное число под видом
измерения. Такие имена остаются непрайсуемыми и всплывают в `chat_unpriced_steps_total`.
"""

from __future__ import annotations

import pytest

from app.pricing.provider_prices import (
    CHAT_TOKEN_PRICES,
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    chat_cost_usd,
    chat_cost_usd_by_provider,
    resolve_chat_price_model,
)


def _usage(model: str) -> dict[str, object]:
    """Один шаг с фиксированными счётчиками: сравниваем ИМЕНА, а не арифметику."""
    return {
        "model": model,
        "inputTokens": 1000,
        "outputTokens": 200,
        "cacheReadTokens": 400,
        "cacheWriteTokens": 100,
    }


# ============================ инвариант: ключ резолвится в себя ============================


@pytest.mark.parametrize("key", sorted(CHAT_TOKEN_PRICES))
def test_every_price_key_resolves_to_itself(key: str) -> None:
    """Сплошной проход по ВСЕМ ключам таблицы, а не по выборке.

    Резолв не имеет права переписать имя, которое таблица уже знает, — иначе он менял бы прайс
    существующих моделей. Отдельно проверяется `claude-haiku-4-5-20251001`: этот ключ САМ выглядит
    как снапшот, и точное совпадение обязано срабатывать раньше префиксного поиска.
    """
    assert resolve_chat_price_model(key) == key


def test_the_table_contains_a_dated_key_so_the_invariant_above_is_not_vacuous() -> None:
    assert any(key[-9:].lstrip("-").isdigit() for key in CHAT_TOKEN_PRICES)


# ============================ снапшот → алиас, обе формы даты ============================


@pytest.mark.parametrize(
    ("snapshot", "alias"),
    [
        ("gpt-5.1-2025-11-13", "gpt-5.1"),
        ("gpt-4o-2024-11-20", "gpt-4o"),
        ("gpt-5-2025-08-07", "gpt-5"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("claude-opus-4-6-20260101", "claude-opus-4-6"),
    ],
)
def test_dated_snapshot_resolves_to_its_alias(snapshot: str, alias: str) -> None:
    assert resolve_chat_price_model(snapshot) == alias


def test_longest_alias_wins() -> None:
    """`gpt-5-mini-…` принадлежит `gpt-5-mini`, а НЕ `gpt-5`: иначе цена завышена в 5 раз."""
    assert resolve_chat_price_model("gpt-5-mini-2025-08-07") == "gpt-5-mini"
    assert CHAT_TOKEN_PRICES["gpt-5-mini"].input_usd < CHAT_TOKEN_PRICES["gpt-5"].input_usd


# ============================ недатированный суффикс → None ============================


@pytest.mark.parametrize(
    "unknown",
    [
        "gpt-5-pro-2025-10-06",  # другая модель, у неё свой прайс — дата НЕ делает её `gpt-5`
        "gpt-5.1-chat-latest",  # алиас-указатель, не снапшот
        "gpt-5-mini-latest",
        "gpt-5-2025-13",  # обрезанная дата — не дата
        "gpt-5-20251",  # 5 цифр — ни одна из двух форм
        "gpt-5x-2025-01-01",  # суффикс не начинается с дефиса после алиаса
        "totally-unknown-model",
        "",
    ],
)
def test_non_dated_suffix_stays_unpriced(unknown: str) -> None:
    """`None` ≠ 0: непрайсуемое имя обязано остаться непрайсуемым, а не подобрать чужую ставку."""
    assert resolve_chat_price_model(unknown) is None
    assert chat_cost_usd([_usage(unknown)]) is None


# ============================ эквивалентность сумм ============================


@pytest.mark.parametrize(
    ("snapshot", "alias", "provider"),
    [
        ("gpt-5.1-2025-11-13", "gpt-5.1", PROVIDER_OPENAI),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5", PROVIDER_ANTHROPIC),
    ],
)
def test_snapshot_costs_exactly_what_the_alias_costs(
    snapshot: str, alias: str, provider: str
) -> None:
    """Именно это чинит накопленную историю: старый ход со снапшотом стоит столько же."""
    by_snapshot = chat_cost_usd([_usage(snapshot)])
    by_alias = chat_cost_usd([_usage(alias)])
    assert by_alias is not None
    assert by_snapshot == pytest.approx(by_alias)

    split_snapshot = chat_cost_usd_by_provider([_usage(snapshot)])
    assert split_snapshot is not None
    assert set(split_snapshot) == {provider}
    assert split_snapshot[provider] == pytest.approx(by_alias)


def test_a_turn_mixing_snapshot_and_alias_of_the_same_model_sums_both_calls() -> None:
    """Ход tool-loop’а мог начаться до правки и продолжиться после: суммируются оба вызова."""
    turn = [_usage("gpt-5.1-2025-11-13"), _usage("gpt-5.1")]
    one = chat_cost_usd([_usage("gpt-5.1")])
    assert one is not None
    assert chat_cost_usd(turn) == pytest.approx(2 * one)


def test_one_unpriceable_call_nulls_the_whole_turn_even_next_to_a_resolved_snapshot() -> None:
    """Частичная сумма занижала бы себестоимость, выглядя полной, — поэтому весь ход `None`."""
    assert chat_cost_usd([_usage("gpt-5.1-2025-11-13"), _usage("gpt-5-pro-2025-10-06")]) is None
