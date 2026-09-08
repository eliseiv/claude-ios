"""Точные ячейки цены медиа и вывод легаси-множителей (ADR-099 §4.4).

Пользовательский контракт `GET /v1/media/models` сегодня МУЛЬТИПЛИКАТИВЕН: клиент считает цену
сам из ``credits`` × пачки × ``resolutionMultipliers[res]`` × ``audioMultiplier``. Развёрнутая
поячеечная таблица в общем случае этой тройкой не представима — оператор вправе поднять цену
`4k`, не трогая `720p`, и целочисленного множителя больше не существует. Выпущенные сборки
приложений мы обновить не можем, поэтому норма единая:

> **Агрегат, который читает клиент, НИКОГДА не занижает фактическое списание. Точное значение
> доступно в аддитивном поле-ячейке.**

⚠️ **Максимум отношений ячеек здесь НЕВЕРЕН.** У ``kling-video-v3`` звуковые ячейки —
`ceil(23×1.5)=35`, `ceil(46×1.5)=69`, `ceil(69×1.5)=104`; максимум отношений даёт `35/23 ≈
1.5217` вместо `1.5`, и клиентская формула выдаёт 70 вместо 69 и 105 вместо 104 — расхождение в
10 из 26 комбинаций НА ДЕФОЛТНОЙ ТАБЛИЦЕ. Метрика непредставимости стала бы постоянным ложным
сигналом, а ложный сигнал обесценивает канал ровно так же, как молчание. Правило «минимальное
значение, не занижающее ПОСЛЕ клиентского `ceil`» на тех же данных даёт ровно `1.5`.

⚠️ **У фото деградации НЕТ.** ``resolutionCredits`` — уже поячеечная карта, поэтому любая
правка фото-ячейки отображается точно. Копировать «безопасное занижение» на фото не нужно, а
копировать «всё точно» на видео — нельзя.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.instance_config.snapshot import InstanceConfigSnapshot, get_snapshot
from app.instance_config.tariffs import (
    VideoCell,
    has_audio_dimension,
    media_run_price,
    photo_unit_credits,
    video_cells,
)
from app.media_generation.catalog import KIND_IMAGE, FalModel, price_multiplier
from app.observability.logging import log_event
from app.observability.metrics import media_price_legacy_overquote

logger = logging.getLogger("app.instance_config.media_pricing")

# Шаг сетки допустимых значений ``audioMultiplier``: 1/20 = 0.05. Сетка нужна, чтобы поле
# оставалось коротким десятичным числом, каким его читают выпущенные сборки.
_AUDIO_STEP_DENOMINATOR = 20


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


@dataclass(frozen=True)
class PriceCell:
    """Точная ячейка цены — то, что спишется на самом деле."""

    resolution: str | None
    duration_seconds: int | None
    audio: bool | None
    credits: int


@dataclass(frozen=True)
class LegacyVideoPricing:
    """Легаси-тройка, выведенная из фактической таблицы ячеек."""

    credits: int
    resolution_multipliers: dict[str, int] | None
    audio_multiplier: int | float | None
    over_quotes: bool


def photo_price_cells(
    model: FalModel,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> list[PriceCell]:
    return [
        PriceCell(
            resolution=resolution,
            duration_seconds=None,
            audio=None,
            credits=photo_unit_credits(model, resolution, settings=settings, snapshot=snapshot),
        )
        for resolution in model.resolution_credits
    ]


def photo_resolution_credits(
    model: FalModel,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> dict[str, int]:
    """Поячеечная карта цен изображения — точная и после операторской правки."""
    return {
        resolution: photo_unit_credits(model, resolution, settings=settings, snapshot=snapshot)
        for resolution in model.resolution_credits
    }


def _cell_price(cell: VideoCell, settings: Settings, snapshot: InstanceConfigSnapshot) -> int:
    return media_run_price(
        model=cell.model,
        duration=cell.duration,
        resolution=cell.resolution,
        generate_audio=cell.audio,
        settings=settings,
        snapshot=snapshot,
    )


def video_price_cells(
    model: FalModel,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> list[PriceCell]:
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    return [
        PriceCell(
            resolution=cell.resolution,
            duration_seconds=cell.seconds,
            audio=cell.audio if has_audio_dimension(model) else None,
            credits=_cell_price(cell, cfg, snap),
        )
        for cell in video_cells(model)
    ]


def _base_resolution(model: FalModel) -> str | None:
    """Разрешение с наименьшим множителем реестра; ``None`` — измерения у модели нет."""
    table = model.resolution_multipliers
    if not table:
        return None
    return min(table, key=lambda key: (table[key], list(table).index(key)))


def _video_cell_prices(
    model: FalModel, cfg: Settings, snap: InstanceConfigSnapshot
) -> tuple[tuple[VideoCell, ...], dict[str, int], dict[str, int]]:
    """Ячейки модели с их фактической ценой и размером пачки — общий вход обоих выводов."""
    cells = video_cells(model)
    prices = {cell.tariff_id: _cell_price(cell, cfg, snap) for cell in cells}
    packs = {
        cell.tariff_id: price_multiplier(model=model, num_images=None, duration=cell.duration)
        for cell in cells
    }
    return cells, prices, packs


def _base_pack_credits(
    cells: tuple[VideoCell, ...],
    *,
    prices: dict[str, int],
    packs: dict[str, int],
    base_resolution: str | None,
) -> int:
    """Цена ОДНОЙ базовой пачки, выведенная из ячеек (§4.4, шаг 1).

    Минимальное значение, покрывающее базовое качество на КАЖДОЙ длительности. Единственное
    определение этого шага: его зовут и вывод легаси-тройки, и базовая цена показа, — иначе у
    одной величины появилось бы два способа вычисления, расходящихся при первой же правке.
    """
    return max(
        1,
        max(
            (
                _ceil_div(prices[cell.tariff_id], packs[cell.tariff_id])
                for cell in cells
                if not cell.audio and cell.resolution == base_resolution
            ),
            default=1,
        ),
    )


def media_base_credits(
    model: FalModel,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> int:
    """Базовая цена запуска ДЛЯ ПОКАЗА — из тех же ячеек, по которым пойдёт списание.

    Это то число, которым подписан выбор модели («от N кредитов») и скалярное поле `credits`
    пользовательского каталога. Реестровая база (``base_credits_for``) на эту роль не годится:
    она не читает операторский тариф, и первая же правка ячейки развела бы показанное и
    списанное — цена не может быть объявлена по одной величине и списана по другой (§5.1).

    - **Фото:** цена ячейки того разрешения, по которому тарифицируется запуск без явно
      выбранного качества (та же лестница фолбэка, что в ``photo_unit_credits``), — у фото
      деградации нет, поячеечная карта точна (§4.4).
    - **Видео:** цена одной базовой пачки, выведенная из ячеек шагом 1 §4.4. Тот же вывод, что
      у легаси-тройки, но без её побочных эффектов: метрика непредставимости и лог-переход
      принадлежат пользовательскому каталогу, а не мастеру выбора параметров в чате.

    На пустом оверлее значение совпадает с прежним ``base_credits_for`` бит-в-бит для каждой
    модели каталога — свойство конструкции: ячейка `1K` фото равна базе по построению
    ``image_unit_credits``, а базовая пачка видео равна `ceil(base × пачки / пачки)`.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    if model.kind == KIND_IMAGE:
        return photo_unit_credits(model, None, settings=cfg, snapshot=snap)
    cells, prices, packs = _video_cell_prices(model, cfg, snap)
    return _base_pack_credits(
        cells, prices=prices, packs=packs, base_resolution=_base_resolution(model)
    )


def derive_legacy_video_pricing(
    model: FalModel,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> LegacyVideoPricing:
    """Минимальная тройка, не занижающая НИ ОДНУ ячейку после клиентского округления.

    Порядок вывода фиксирован — каждый шаг опирается только на уже вычисленное:

    1. ``credits`` = максимум по длительностям от `ceil(ячейка(base_r, без звука) / пачки)`:
       покрывает базовое качество на КАЖДОЙ длительности;
    2. ``resolutionMultipliers[r]`` = минимальное ЦЕЛОЕ (тип поля — `int`), не занижающее ни
       одну ячейку этого разрешения;
    3. ``audioMultiplier`` = НАИМЕНЬШЕЕ кратное 1/20, при котором клиентская формула не
       занижает ни одну звуковую ячейку; проверка в ЦЕЛОЧИСЛЕННОЙ арифметике.

    У модели без измерения поле остаётся ``None``, а не нейтральной единицей: подставить `1`
    значило бы объявить измерение, которого у модели нет.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    cells, prices, packs = _video_cell_prices(model, cfg, snap)
    base_resolution = _base_resolution(model)
    credits = _base_pack_credits(cells, prices=prices, packs=packs, base_resolution=base_resolution)

    multipliers: dict[str, int] | None = None
    if model.resolution_multipliers:
        multipliers = {}
        for resolution in model.resolution_multipliers:
            multipliers[resolution] = max(
                (
                    max(
                        1,
                        _ceil_div(prices[cell.tariff_id], credits * packs[cell.tariff_id]),
                    )
                    for cell in cells
                    if not cell.audio and cell.resolution == resolution
                ),
                default=1,
            )

    audio_multiplier: int | float | None = None
    audio_cells = [cell for cell in cells if cell.audio]
    if audio_cells:
        audio_multiplier = _minimal_audio_multiplier(
            audio_cells, prices=prices, packs=packs, credits=credits, multipliers=multipliers
        )

    over_quotes = _detects_over_quote(
        cells,
        prices=prices,
        packs=packs,
        credits=credits,
        multipliers=multipliers,
        audio_multiplier=audio_multiplier,
    )
    media_price_legacy_overquote.labels(model=model.id).set(1 if over_quotes else 0)
    _report_representability(model.id, over_quotes=over_quotes)
    return LegacyVideoPricing(
        credits=credits,
        resolution_multipliers=multipliers,
        audio_multiplier=audio_multiplier,
        over_quotes=over_quotes,
    )


# Последнее ЗАЛОГИРОВАННОЕ состояние представимости по модели. Гейдж обновляется на каждом
# вызове (его читают скрейпом), а ЛОГ пишется только на переходе: вывод тройки выполняется на
# КАЖДОМ обращении к пользовательскому каталогу, и строка на каждый вызов превратила бы событие
# в поток, в котором сам переход уже не виден. Обесценивание канала стоит ровно столько же,
# сколько молчание, — тот же приём, что у состава оверлеев в `snapshot.install_snapshot`.
_reported_non_representable: dict[str, bool] = {}


def _report_representability(model_id: str, *, over_quotes: bool) -> None:
    # Отсутствие записи трактуется как «представима»: это НОРМАТИВНОЕ состояние на дефолтной
    # таблице, поэтому первое наблюдение представимости — не событие и лога не заслуживает.
    # Первое же наблюдение НЕпредставимости событием является и пишется.
    if _reported_non_representable.get(model_id, False) == over_quotes:
        return
    _reported_non_representable[model_id] = over_quotes
    if over_quotes:
        log_event(logger, logging.WARNING, "media_price_table_non_representable", model=model_id)
    else:
        log_event(logger, logging.INFO, "media_price_table_representable_again", model=model_id)


def _resolution_factor(cell: VideoCell, multipliers: dict[str, int] | None) -> int:
    if multipliers is None or cell.resolution is None:
        return 1
    return multipliers.get(cell.resolution, 1)


def _minimal_audio_multiplier(
    audio_cells: list[VideoCell],
    *,
    prices: dict[str, int],
    packs: dict[str, int],
    credits: int,
    multipliers: dict[str, int] | None,
) -> int | float:
    """Наименьшее кратное 1/20, не занижающее ни одну звуковую ячейку ПОСЛЕ клиентского `ceil`.

    Именно «наименьшее», а не «максимум отношений»: отношения берутся от УЖЕ ОКРУГЛЁННЫХ ячеек
    и потому завышают — на дефолтной таблице это дало бы 1.5217 вместо 1.5 и сдвинуло бы поле
    контракта в день выката.

    Значение вычисляется ЗАКРЫТОЙ ФОРМУЛОЙ, а не перебором: клиентская формула не занижает
    ячейку `P` при базе `X` ровно тогда, когда `ceil(X·m/20) ≥ P`, то есть `X·m > 20·(P−1)`,
    то есть `m ≥ ⌊20·(P−1)/X⌋ + 1`. Максимум этих границ по звуковым ячейкам и есть ответ.
    """
    minimal = 1
    for cell in audio_cells:
        base = credits * packs[cell.tariff_id] * _resolution_factor(cell, multipliers)
        if base <= 0:  # pragma: no cover — credits и пачки всегда положительны
            continue
        needed = (_AUDIO_STEP_DENOMINATOR * (prices[cell.tariff_id] - 1)) // base + 1
        minimal = max(minimal, needed)
    # Целое кратное отдаётся целым: поле годами уходило как `2`, и расширение до `2.0`
    # сломало бы клиента, декодирующего его как int.
    if minimal % _AUDIO_STEP_DENOMINATOR == 0:
        return minimal // _AUDIO_STEP_DENOMINATOR
    return minimal / _AUDIO_STEP_DENOMINATOR


def legacy_client_price(
    cell: VideoCell,
    *,
    credits: int,
    multipliers: dict[str, int] | None,
    audio_multiplier: int | float | None,
    packs: int,
) -> int:
    """Цена, которую посчитает ВЫПУЩЕННАЯ сборка по легаси-тройке."""
    base = credits * packs * _resolution_factor(cell, multipliers)
    if not cell.audio or audio_multiplier is None:
        return base
    step = round(audio_multiplier * _AUDIO_STEP_DENOMINATOR)
    return _ceil_div(base * step, _AUDIO_STEP_DENOMINATOR)


def _detects_over_quote(
    cells: tuple[VideoCell, ...],
    *,
    prices: dict[str, int],
    packs: dict[str, int],
    credits: int,
    multipliers: dict[str, int] | None,
    audio_multiplier: int | float | None,
) -> bool:
    return any(
        legacy_client_price(
            cell,
            credits=credits,
            multipliers=multipliers,
            audio_multiplier=audio_multiplier,
            packs=packs[cell.tariff_id],
        )
        > prices[cell.tariff_id]
        for cell in cells
    )
