"""Себестоимость запроса в USD — закупочные прайсы провайдеров (ADR-079).

Что здесь закреплено:

- чат считается ТОЧНО по токенам, причём кэш у двух провайдеров живёт в разных местах отчёта:
  у OpenAI кэшированный префикс ВХОДИТ в `inputTokens`, у Anthropic — нет. Смешать их значит
  посчитать кэш дважды (OpenAI) или потерять его (Anthropic);
- ход tool-loop’а стоит СУММУ своих вызовов: провайдер выставил счёт за каждый;
- цены, которой нет, не существует: неизвестная модель и шаг без счётчиков токенов дают
  `None`, а не `0.0` («бесплатный запрос» — это измерение, и оно было бы ложным);
- медиа: точная цена по фактическим параметрам запуска совпадает с закупочной таблицей fal
  (ADR-061), а восстановление из кредитов помечает оценку сверху там, где fal тарифицирует
  мельче нашей пачки.
"""

from __future__ import annotations

import pytest

from app.media_generation.catalog import find_model
from app.pricing.provider_prices import (
    chat_cost_usd,
    media_cost_usd_from_credits,
    media_cost_usd_of_run,
    round_usd,
)


def _model(model_id: str):  # type: ignore[no-untyped-def]
    model = find_model(model_id)
    assert model is not None, model_id
    return model


# ================================ chat ================================


def test_openai_cache_is_subtracted_from_input_not_added_to_it() -> None:
    """gpt-4o: 1000 входных, из них 400 кэш → 600×$2.5 + 400×$1.25 + 200×$10 за 1M."""
    usage = {
        "model": "gpt-4o",
        "inputTokens": 1000,
        "outputTokens": 200,
        "cacheReadTokens": 400,
        "cacheWriteTokens": 0,
    }
    expected = (600 * 2.50 + 400 * 1.25 + 200 * 10.00) / 1_000_000
    assert chat_cost_usd([usage]) == pytest.approx(expected)


def test_anthropic_cache_is_added_to_input_because_the_report_excludes_it() -> None:
    """У Anthropic `input_tokens` НЕ содержит кэш, поэтому вычитать нечего — только складывать."""
    usage = {
        "model": "claude-sonnet-4-5",
        "inputTokens": 1000,
        "outputTokens": 200,
        "cacheReadTokens": 400,
        "cacheWriteTokens": 100,
    }
    expected = (1000 * 3.00 + 400 * 0.30 + 100 * 3.75 + 200 * 15.00) / 1_000_000
    assert chat_cost_usd([usage]) == pytest.approx(expected)


def test_tool_loop_turn_costs_the_sum_of_its_calls() -> None:
    steps = [
        {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
        {"model": "gpt-4o", "inputTokens": 2000, "outputTokens": 50},
    ]
    single = (1000 * 2.50 + 100 * 10.00) / 1_000_000
    second = (2000 * 2.50 + 50 * 10.00) / 1_000_000
    assert chat_cost_usd(steps) == pytest.approx(single + second)


def test_web_search_fee_is_added_per_request() -> None:
    """$10 за 1000 поисков у обоих провайдеров = $0.01 за поиск."""
    usage = {"model": "gpt-4o", "inputTokens": 0, "outputTokens": 0, "webSearchRequests": 3}
    assert chat_cost_usd([usage]) == pytest.approx(0.03)


@pytest.mark.parametrize(
    ("name", "usages"),
    [
        ("нет ни одного вызова", []),
        ("модель вне прайса", [{"model": "gpt-9-unreleased", "inputTokens": 10}]),
        ("шаг без счётчиков токенов", [{"model": "gpt-4o"}]),
    ],
)
def test_unpriceable_turn_is_none_never_zero(name: str, usages: list[dict[str, object]]) -> None:
    assert chat_cost_usd(usages) is None, name


def test_one_unpriced_call_makes_the_whole_turn_unmeasured() -> None:
    """Частичная сумма выглядела бы как полная — поэтому вся строка становится «не измерено»."""
    steps = [
        {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
        {"model": "some-byok-model", "inputTokens": 1000, "outputTokens": 100},
    ]
    assert chat_cost_usd(steps) is None


# ================================ media: точная цена запуска ================================


@pytest.mark.parametrize(
    ("model_id", "kwargs", "expected"),
    [
        # Закупочная таблица ADR-061 §«Закупочные цены fal», строка за строкой.
        ("nano-banana-2", {"resolution": "0.5K", "num_images": 1}, 0.06),
        ("nano-banana-2", {"resolution": "4K", "num_images": 4}, 0.64),
        ("nano-banana-pro", {"resolution": "1K", "num_images": 1}, 0.15),
        ("nano-banana-pro", {"resolution": "2K", "num_images": 1}, 0.15),
        ("nano-banana-pro", {"resolution": "4K", "num_images": 2}, 0.60),
        ("kling-video", {"duration": "5"}, 0.35),
        ("kling-video", {"duration": "10"}, 0.70),
        ("kling-video-v3", {"duration": "5"}, 0.56),
        ("kling-video-v3", {"duration": "3"}, 0.336),
        ("kling-video-v3", {"duration": "5", "generate_audio": True}, 0.84),
        ("veo-3.1", {"duration": "8s", "resolution": "720p"}, 1.60),
        ("veo-3.1", {"duration": "6s", "resolution": "1080p"}, 1.20),
        ("veo-3.1", {"duration": "8s", "resolution": "4k", "generate_audio": True}, 4.80),
    ],
)
def test_exact_run_cost_matches_the_fal_price_list(
    model_id: str, kwargs: dict[str, object], expected: float
) -> None:
    cost = media_cost_usd_of_run(model=_model(model_id), **kwargs)  # type: ignore[arg-type]
    assert cost == pytest.approx(expected)


def test_run_without_duration_has_no_cost_rather_than_a_guessed_one() -> None:
    assert media_cost_usd_of_run(model=_model("veo-3.1"), duration=None) is None


# ========================== media: восстановление из кредитов ==========================


def test_image_tiers_with_one_price_per_credit_need_no_asset_count() -> None:
    """nano-banana-2: $0.02 за кредит на ВСЕХ ступенях, поэтому разбивка не влияет на ответ."""
    cost = media_cost_usd_from_credits(
        model=_model("nano-banana-2"), base_credits=4, credits_charged=32, asset_count=None
    )
    assert (cost.usd, cost.estimated) == (pytest.approx(0.64), False)


def test_asset_count_resolves_the_resolution_of_a_nonlinear_image_tier() -> None:
    """nano-banana-pro: 64 кредита на 4 картинки = 16 за кадр = 4K = $0.30 × 4."""
    cost = media_cost_usd_from_credits(
        model=_model("nano-banana-pro"), base_credits=8, credits_charged=64, asset_count=4
    )
    assert (cost.usd, cost.estimated) == (pytest.approx(1.20), False)


def test_failed_image_run_without_assets_is_an_upper_bound_not_a_fact() -> None:
    """У провалившегося запуска ассетов нет, ступени спорят о цене → оценка сверху."""
    cost = media_cost_usd_from_credits(
        model=_model("nano-banana-pro"), base_credits=8, credits_charged=24, asset_count=None
    )
    assert cost.estimated is True
    assert cost.usd == pytest.approx(24 * 0.15 / 8)


def test_kling_25_credits_convert_exactly_because_it_sells_whole_packs() -> None:
    cost = media_cost_usd_from_credits(
        model=_model("kling-video"), base_credits=14, credits_charged=28, asset_count=1
    )
    assert (cost.usd, cost.estimated) == (pytest.approx(0.70), False)


@pytest.mark.parametrize(
    ("model_id", "base", "credits", "expected"),
    [
        ("kling-video-v3", 23, 104, 104 * (0.56 / 23)),
        ("veo-3.1", 32, 64, 64 * (0.80 / 32)),
    ],
)
def test_per_second_video_recovered_from_credits_is_marked_estimated(
    model_id: str, base: int, credits: int, expected: float
) -> None:
    """fal берёт за секунды внутри пачки, а в строке осталась только пачка — это оценка сверху."""
    cost = media_cost_usd_from_credits(
        model=_model(model_id), base_credits=base, credits_charged=credits, asset_count=1
    )
    assert cost.estimated is True
    assert cost.usd == pytest.approx(expected)


def test_zero_credits_have_no_cost_to_recover() -> None:
    cost = media_cost_usd_from_credits(
        model=_model("veo-3.1"), base_credits=32, credits_charged=0, asset_count=None
    )
    assert cost.usd is None


def test_round_usd_keeps_sub_cent_values_visible() -> None:
    assert round_usd(0.0000004) == pytest.approx(0.0)
    assert round_usd(0.00012345678) == pytest.approx(0.000123)
    assert round_usd(None) is None
