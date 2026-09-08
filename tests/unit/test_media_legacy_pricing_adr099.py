"""Unit: численная модель легаси-множителей медиа (ADR-099 §4.4).

Пользовательский контракт `GET /v1/media/models` МУЛЬТИПЛИКАТИВЕН: выпущенная сборка считает цену
сама из ``credits`` × пачки × ``resolutionMultipliers[r]`` × ``audioMultiplier``. Поячеечная
таблица этой тройкой в общем случае не представима, и норма одна:

> **Агрегат, который читает клиент, НИКОГДА не занижает фактическое списание.**

Кейсы ниже проверяют обе стороны нормы и обязаны падать при возврате к «максимуму отношений»
(на дефолтной таблице это даёт 70 вместо 69) и при замене округления вверх на вниз.
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Any

import pytest

from app.config import Settings
from app.instance_config.media_pricing import (
    derive_legacy_video_pricing,
    legacy_client_price,
    photo_price_cells,
    photo_resolution_credits,
    video_price_cells,
)
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    TariffOverlay,
)
from app.instance_config.tariffs import has_audio_dimension, photo_cells, video_cells
from app.media_generation.catalog import (
    KIND_IMAGE,
    KIND_VIDEO,
    image_unit_credits,
    models_of_kind,
    price_multiplier,
)
from app.observability.metrics import media_price_legacy_overquote

_NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.UTC)
_VIDEO_MODELS = models_of_kind(KIND_VIDEO)
_IMAGE_MODELS = models_of_kind(KIND_IMAGE)


@pytest.fixture(autouse=True)
def _enable_instance_config_loggers() -> None:
    """Вернуть логгеры `app.instance_config*` во включённое состояние.

    ⚠️ Alembic-миграция вызывает ``fileConfig("alembic.ini")`` с
    ``disable_existing_loggers=True`` и выключает КАЖДЫЙ `app.*`-логгер на весь процесс
    (см. докстроку фикстуры ``_migrated`` в ``tests/conftest.py``). В одиночном прогоне
    ``pytest tests/unit`` эта фикстура не срабатывает вовсе, поэтому кейсы на логах зелены; в
    полном прогоне CI (`tests/e2e` и `tests/integration` собираются РАНЬШЕ `tests/unit`)
    ``caplog.records`` оказывается пустым — тест на НАЛИЧИЕ строки падает, а тест на ОТСУТСТВИЕ
    строки проходит ВХОЛОСТУЮ. Тот же приём уже применён в
    ``test_billing_cloudpayments_payment_type_fallback_adr057.py``.
    """
    for name in ("app.instance_config", "app.instance_config.media_pricing"):
        logging.getLogger(name).disabled = False


def _settings(**kwargs: Any) -> Settings:
    return Settings(
        **{
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "FAL_API_KEY": "fal-test-key",
            **kwargs,
        }
    )


def _overlay(**tokens: int) -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        tariffs={
            tariff_id: TariffOverlay(tariff_id=tariff_id, tokens=value, updated_at=_NOW)
            for tariff_id, value in tokens.items()
        }
    )


def _gauge(model_id: str) -> float:
    return media_price_legacy_overquote.labels(model=model_id)._value.get()  # noqa: SLF001


@pytest.fixture(autouse=True)
def _reset_representability_log_state() -> Any:
    """Лог представимости пишется НА ПЕРЕХОДЕ и потому держит состояние процесса.

    Без сброса порядок тестов решал бы, увидит ли кейс строку, — то есть тест стал бы флапающим
    по причине, к предмету отношения не имеющей.
    """
    from app.instance_config import media_pricing

    media_pricing._reported_non_representable.clear()  # noqa: SLF001
    yield
    media_pricing._reported_non_representable.clear()  # noqa: SLF001


# ============================== дефолтная таблица представима ===============================
@pytest.mark.parametrize("model", _VIDEO_MODELS, ids=lambda m: m.id)
def test_default_table_derives_exactly_the_registry_triple(model: Any) -> None:
    """На дефолтах выведенная тройка совпадает со значениями реестра ПОЭЛЕМЕНТНО.

    Значения не переписаны из документа: сравнение идёт с самим реестром. Кейс падает при
    возврате к «максимуму отношений» — он дал бы `audioMultiplier ≈ 1.5217` вместо `1.5`.
    """
    derived = derive_legacy_video_pricing(model, settings=_settings(), snapshot=EMPTY_SNAPSHOT)

    assert derived.credits == model.default_credits
    if model.resolution_multipliers:
        assert derived.resolution_multipliers == dict(model.resolution_multipliers)
    else:
        assert derived.resolution_multipliers is None
    if has_audio_dimension(model):
        assert derived.audio_multiplier == model.audio_multiplier
    else:
        assert derived.audio_multiplier is None


@pytest.mark.parametrize("model", _VIDEO_MODELS, ids=lambda m: m.id)
def test_default_table_reproduces_every_cell_exactly_and_the_gauge_is_zero(model: Any) -> None:
    """Ни одного занижения И ни одного завышения: клиент ничего не замечает.

    Ноль на дефолтах — НОРМАТИВНОЕ состояние метрики, а не «обычно ноль»: единица на нетронутом
    инстансе означала бы дефект вывода, а постоянный ложный сигнал обесценивает канал ровно так
    же, как молчание.
    """
    settings = _settings()
    derived = derive_legacy_video_pricing(model, settings=settings, snapshot=EMPTY_SNAPSHOT)
    actual = {
        (cell.resolution, cell.duration_seconds, cell.audio): cell.credits
        for cell in video_price_cells(model, settings=settings, snapshot=EMPTY_SNAPSHOT)
    }
    assert actual  # предусловие: у модели есть комбинации

    for cell in video_cells(model):
        client = legacy_client_price(
            cell,
            credits=derived.credits,
            multipliers=derived.resolution_multipliers,
            audio_multiplier=derived.audio_multiplier,
            packs=price_multiplier(model=model, num_images=None, duration=cell.duration),
        )
        key = (cell.resolution, cell.seconds, cell.audio if has_audio_dimension(model) else None)
        assert client == actual[key], f"{cell.tariff_id}: клиент {client} ≠ ячейка {actual[key]}"

    assert derived.over_quotes is False
    assert _gauge(model.id) == 0


def test_all_video_cells_of_the_instance_are_reproduced_exactly() -> None:
    """Сводный кейс §Тесты п.4: суммарно по всем моделям реестра — ноль расхождений."""
    settings = _settings()
    checked = 0

    for model in _VIDEO_MODELS:
        derived = derive_legacy_video_pricing(model, settings=settings, snapshot=EMPTY_SNAPSHOT)
        actual = {
            (cell.resolution, cell.duration_seconds, cell.audio): cell.credits
            for cell in video_price_cells(model, settings=settings, snapshot=EMPTY_SNAPSHOT)
        }
        for cell in video_cells(model):
            key = (
                cell.resolution,
                cell.seconds,
                cell.audio if has_audio_dimension(model) else None,
            )
            assert (
                legacy_client_price(
                    cell,
                    credits=derived.credits,
                    multipliers=derived.resolution_multipliers,
                    audio_multiplier=derived.audio_multiplier,
                    packs=price_multiplier(model=model, num_images=None, duration=cell.duration),
                )
                == actual[key]
            )
            checked += 1

    assert checked > 0


def test_the_max_of_ratios_rule_would_break_the_default_table() -> None:
    """Diff-кейс правила: «максимум отношений» НЕВЕРЕН, и это не теоретическая придирка.

    Отношения берутся от УЖЕ ОКРУГЛЁННЫХ ячеек и потому завышают. Обе величины вычисляются здесь
    из реестра, а не переписаны из документа: кейс доказывает, что альтернативная формула даёт
    расхождение на дефолтах, то есть сдвинула бы поле контракта в день выката.
    """
    settings = _settings()
    model = next(
        m for m in _VIDEO_MODELS if has_audio_dimension(m) and not m.resolution_multipliers
    )
    derived = derive_legacy_video_pricing(model, settings=settings, snapshot=EMPTY_SNAPSHOT)
    prices = {
        cell.tariff_id: credits
        for cell, credits in zip(
            video_cells(model),
            [
                c.credits
                for c in video_price_cells(model, settings=settings, snapshot=EMPTY_SNAPSHOT)
            ],
            strict=True,
        )
    }

    ratios_rule = max(
        prices[cell.tariff_id]
        / (derived.credits * price_multiplier(model=model, num_images=None, duration=cell.duration))
        for cell in video_cells(model)
        if cell.audio
    )

    assert derived.audio_multiplier is not None
    assert ratios_rule > derived.audio_multiplier  # завышает

    # Считаем так, как считает ВЫПУЩЕННАЯ сборка: `ceil(база × множитель)` над тем числом, которое
    # реально ушло бы в поле контракта, — а не по нашей сетке 1/20 (она бы это завышение скрыла).
    def _released_client(multiplier: float, cell: Any) -> int:
        base = derived.credits * price_multiplier(
            model=model, num_images=None, duration=cell.duration
        )
        return math.ceil(base * multiplier)

    audio_cells = [cell for cell in video_cells(model) if cell.audio]
    diverged = [
        cell
        for cell in audio_cells
        if _released_client(ratios_rule, cell) != prices[cell.tariff_id]
    ]
    assert diverged, "альтернативное правило обязано расходиться — иначе кейс вырожден"
    # …тогда как принятое правило воспроизводит КАЖДУЮ звуковую ячейку точно.
    assert all(
        _released_client(derived.audio_multiplier, cell) == prices[cell.tariff_id]
        for cell in audio_cells
    )


def test_a_model_without_a_dimension_keeps_null_not_a_neutral_one() -> None:
    """Подставить `1` значило бы объявить измерение, которого у модели нет."""
    settings = _settings()
    silent = [m for m in _VIDEO_MODELS if not has_audio_dimension(m)]
    flat = [m for m in _VIDEO_MODELS if not m.resolution_multipliers]
    assert silent and flat  # предусловие: такие модели в реестре есть

    for model in silent:
        assert (
            derive_legacy_video_pricing(
                model, settings=settings, snapshot=EMPTY_SNAPSHOT
            ).audio_multiplier
            is None
        )
    for model in flat:
        assert (
            derive_legacy_video_pricing(
                model, settings=settings, snapshot=EMPTY_SNAPSHOT
            ).resolution_multipliers
            is None
        )


# ============================== непредставимая таблица ======================================
def _raise_one_resolution(model: Any, resolution: str, delta: int) -> InstanceConfigSnapshot:
    settings = _settings()
    cells = video_price_cells(model, settings=settings, snapshot=EMPTY_SNAPSHOT)
    prices = dict(
        zip(
            [c.tariff_id for c in video_cells(model)],
            [c.credits for c in cells],
            strict=True,
        )
    )
    bumped = {
        cell.tariff_id: prices[cell.tariff_id] + delta
        for cell in video_cells(model)
        if cell.resolution == resolution
    }
    return _overlay(**bumped)


def test_raising_only_the_top_resolution_never_underquotes_any_combination() -> None:
    """Расхождение появляется только в БЕЗОПАСНУЮ сторону — показано не меньше, чем спишется.

    Проверяется ПЕРЕБОРОМ всех комбинаций модели, а не одной точкой: занижение хотя бы в одной
    ячейке означает скрытую переплату, ради исключения которой правило и введено.
    """
    settings = _settings()
    model = next(m for m in _VIDEO_MODELS if m.resolution_multipliers)
    top = max(model.resolution_multipliers, key=lambda key: model.resolution_multipliers[key])
    snapshot = _raise_one_resolution(model, top, delta=7)

    derived = derive_legacy_video_pricing(model, settings=settings, snapshot=snapshot)
    actual = dict(
        zip(
            [c.tariff_id for c in video_cells(model)],
            [c.credits for c in video_price_cells(model, settings=settings, snapshot=snapshot)],
            strict=True,
        )
    )

    for cell in video_cells(model):
        client = legacy_client_price(
            cell,
            credits=derived.credits,
            multipliers=derived.resolution_multipliers,
            audio_multiplier=derived.audio_multiplier,
            packs=price_multiplier(model=model, num_images=None, duration=cell.duration),
        )
        assert (
            client >= actual[cell.tariff_id]
        ), f"{cell.tariff_id}: занижение {client} < {actual[cell.tariff_id]}"


def test_a_non_representable_table_raises_the_gauge_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Непредставимость НЕ молчит — и не флудит: лог пишется на ПЕРЕХОДЕ."""
    settings = _settings()
    model = next(m for m in _VIDEO_MODELS if m.resolution_multipliers)
    top = max(model.resolution_multipliers, key=lambda key: model.resolution_multipliers[key])
    snapshot = _raise_one_resolution(model, top, delta=7)

    with caplog.at_level(logging.WARNING, logger="app.instance_config.media_pricing"):
        first = derive_legacy_video_pricing(model, settings=settings, snapshot=snapshot)
        derive_legacy_video_pricing(model, settings=settings, snapshot=snapshot)
        derive_legacy_video_pricing(model, settings=settings, snapshot=snapshot)

    assert first.over_quotes is True
    assert _gauge(model.id) == 1
    lines = [
        r.message for r in caplog.records if r.message == "media_price_table_non_representable"
    ]
    assert len(lines) == 1  # три вывода подряд — одна строка, а не три


def test_three_derivations_on_defaults_emit_no_non_representable_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§Наблюдаемость: лог непредставимости — на переходе, а не на каждом обращении.

    Три подряд сборки каталога на дефолтах не дают НИ ОДНОЙ строки: представимость — нормативное
    состояние, и первое её наблюдение событием не является.
    """
    settings = _settings()

    with caplog.at_level(logging.INFO, logger="app.instance_config.media_pricing"):
        for _ in range(3):
            for model in _VIDEO_MODELS:
                derive_legacy_video_pricing(model, settings=settings, snapshot=EMPTY_SNAPSHOT)

    assert not [r for r in caplog.records if r.message == "media_price_table_non_representable"]
    assert not [r for r in caplog.records if r.message == "media_price_table_representable_again"]

    # ⚠️ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: без него кейс проходит ВХОЛОСТУЮ.
    # Утверждение «строк нет» истинно и тогда, когда канал молчит по постороннней причине —
    # выключенный логгер, не тот логгер, не тот уровень. Тогда тест проверяет не молчание
    # продюсера, а собственную слепоту. Здесь тот же canal заведомо заставляют заговорить.
    model = next(m for m in _VIDEO_MODELS if m.resolution_multipliers)
    top = max(model.resolution_multipliers, key=lambda key: model.resolution_multipliers[key])
    with caplog.at_level(logging.WARNING, logger="app.instance_config.media_pricing"):
        derive_legacy_video_pricing(
            model, settings=settings, snapshot=_raise_one_resolution(model, top, delta=7)
        )
    assert [r for r in caplog.records if r.message == "media_price_table_non_representable"]


def test_returning_to_a_representable_table_is_logged_and_lowers_the_gauge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    model = next(m for m in _VIDEO_MODELS if m.resolution_multipliers)
    top = max(model.resolution_multipliers, key=lambda key: model.resolution_multipliers[key])

    derive_legacy_video_pricing(
        model, settings=settings, snapshot=_raise_one_resolution(model, top, delta=7)
    )
    with caplog.at_level(logging.INFO, logger="app.instance_config.media_pricing"):
        back = derive_legacy_video_pricing(model, settings=settings, snapshot=EMPTY_SNAPSHOT)

    assert back.over_quotes is False
    assert _gauge(model.id) == 0
    assert "media_price_table_representable_again" in {r.message for r in caplog.records}


# ============================== фото: деградации нет ========================================
@pytest.mark.parametrize("model", _IMAGE_MODELS, ids=lambda m: m.id)
def test_photo_map_is_exact_before_and_after_an_operator_edit(model: Any) -> None:
    """⚠️ Контраст, обязанный быть названным: у фото деградации НЕТ.

    `resolutionCredits` — уже поячеечная карта, поэтому любая правка отображается ТОЧНО.
    Копировать «безопасное занижение» на фото не нужно, а «всё точно» на видео — нельзя.
    """
    settings = _settings()
    exact = photo_resolution_credits(model, settings=settings, snapshot=EMPTY_SNAPSHOT)

    assert exact == {
        resolution: image_unit_credits(model, resolution, base_credits=model.default_credits)
        for resolution in model.resolution_credits
    }

    cell = photo_cells(model)[-1]
    snapshot = _overlay(**{cell.tariff_id: 123})
    edited = photo_resolution_credits(model, settings=settings, snapshot=snapshot)
    assert edited[cell.resolution] == 123
    assert [c.credits for c in photo_price_cells(model, settings=settings, snapshot=snapshot)] == [
        edited[resolution] for resolution in model.resolution_credits
    ]
