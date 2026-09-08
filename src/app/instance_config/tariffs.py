"""Реестр тарифных строк и разрешение цены (ADR-099 §3, §4, §5).

**Идентификатор строки — детерминированная функция от координат варианта, а не суррогатный
ключ.** Суррогатный `uuid` потребовал бы засеять таблицу (что запрещено §2) и хранить
сопоставление «uuid ↔ вариант», то есть завести второй дом у той же координаты. Вычислимый
идентификатор переживает перезапуск и выводится из параметров запуска на горячем пути.

**Дефолт каждой строки ВЫЧИСЛЯЕТСЯ из сегодняшнего источника** (`run_price()` для медиа,
`CHAT_CREDIT_COST_GENERAL` для чата), а не переписывается константой: переписанная копия
разошлась бы с формулой при первом же изменении калибровки, и «ни одно списание не изменилось»
пришлось бы доказывать сверкой чисел вместо конструкции.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import Settings, get_settings
from app.instance_config.snapshot import InstanceConfigSnapshot, get_snapshot
from app.media_generation.catalog import (
    KIND_IMAGE,
    KIND_VIDEO,
    FalModel,
    duration_seconds,
    find_model,
    image_unit_credits,
    models_of_kind,
    run_price,
)

KIND_CHAT = "chat"
KIND_PHOTO = "photo"
# `kind` строки контракта: `video` совпадает с внутренним KIND_VIDEO, `photo` — нет
# (внутри модуль генерации называет его `image`), поэтому имя объявлено отдельно.
KIND_VIDEO_ROW = KIND_VIDEO

UNIT_MESSAGE = "message"
UNIT_IMAGE = "image"
UNIT_GENERATION = "generation"

# Отсутствующее у модели измерение — литерал `na`, а не пустой сегмент: пустой сегмент делает
# `a::b` и `a:b` неразличимыми при чтении.
NA = "na"

PROVIDER_FAL = "fal"


def _segment(value: str) -> str:
    return value.strip().lower()


def chat_tariff_id(provider: str, model: str) -> str:
    return f"{KIND_CHAT}:{_segment(provider)}:{_segment(model)}"


def photo_tariff_id(model_id: str, resolution: str) -> str:
    return f"{KIND_PHOTO}:{_segment(model_id)}:{_segment(resolution)}"


def video_tariff_id(model_id: str, resolution: str | None, seconds: int, *, audio: bool) -> str:
    res = _segment(resolution) if resolution else NA
    return f"{KIND_VIDEO_ROW}:{_segment(model_id)}:{res}:{seconds}:{1 if audio else 0}"


def base_credits_for(model: FalModel, settings: Settings | None = None) -> int:
    """Базовая цена запуска модели: операторская карта ``MEDIA_MODEL_CREDITS``, иначе каталог.

    Единственное определение этой величины в сервисе — дефолт тарифной строки и база реестровой
    формулы, когда ячейка не настроена.

    ⚠️ **Это НЕ цена показа.** Тариф оператора (`admin_tariffs`) здесь не читается, поэтому
    отдавать это число пользователю нельзя: после правки ячейки показанное разошлось бы со
    списанным. Для пользовательского ответа — ``media_pricing.media_base_credits``.
    """
    cfg = settings or get_settings()
    return cfg.media_model_credits().get(model.id, model.default_credits)


def has_audio_dimension(model: FalModel) -> bool:
    """Тарифицирует ли модель звук отдельным измерением.

    ``kling-video`` показывает переключатель звука, но не берёт за него денег, поэтому звуковой
    координаты у его строк нет: строка, отличающаяся только флагом и совпадающая ценой, —
    удвоение каталога без единого нового решения для оператора.
    """
    return model.supports_audio and model.audio_multiplier is not None


@dataclass(frozen=True)
class PhotoCell:
    """Ячейка «модель × разрешение»: цена ОДНОГО изображения."""

    model: FalModel
    resolution: str
    tariff_id: str


@dataclass(frozen=True)
class VideoCell:
    """Ячейка «модель × разрешение × длительность × звук»: ПОЛНАЯ цена запуска."""

    model: FalModel
    resolution: str | None
    duration: str
    seconds: int
    audio: bool
    tariff_id: str


def photo_cells(model: FalModel) -> tuple[PhotoCell, ...]:
    return tuple(
        PhotoCell(model=model, resolution=res, tariff_id=photo_tariff_id(model.id, res))
        for res in model.resolution_credits
    )


def video_cells(model: FalModel) -> tuple[VideoCell, ...]:
    """Все комбинации, которые модель умеет запустить.

    Разрешения — из карты множителей реестра (её нет ⇒ измерения нет), длительности — из
    text-варианта (режим text/image на цену не влияет), звук — только если он тарифицируется.
    """
    resolutions: tuple[str | None, ...] = (
        tuple(model.resolution_multipliers) if model.resolution_multipliers else (None,)
    )
    audios: tuple[bool, ...] = (False, True) if has_audio_dimension(model) else (False,)
    cells: list[VideoCell] = []
    for resolution in resolutions:
        for duration in model.text_variant.durations:
            seconds = duration_seconds(duration)
            if seconds is None:
                continue
            for audio in audios:
                cells.append(
                    VideoCell(
                        model=model,
                        resolution=resolution,
                        duration=duration,
                        seconds=seconds,
                        audio=audio,
                        tariff_id=video_tariff_id(model.id, resolution, seconds, audio=audio),
                    )
                )
    return tuple(cells)


def default_photo_cell_price(cell: PhotoCell, settings: Settings | None = None) -> int:
    return image_unit_credits(
        cell.model, cell.resolution, base_credits=base_credits_for(cell.model, settings)
    )


def default_video_cell_price(cell: VideoCell, settings: Settings | None = None) -> int:
    return run_price(
        model=cell.model,
        base_credits=base_credits_for(cell.model, settings),
        duration=cell.duration,
        resolution=cell.resolution,
        generate_audio=cell.audio,
    )


def _overlay_tokens(tariff_id: str, snapshot: InstanceConfigSnapshot) -> int | None:
    row = snapshot.tariffs.get(tariff_id)
    return None if row is None else row.tokens


# --- Горячий путь: разрешение цены -------------------------------------------------------


def chat_turn_credit_cost(
    model: str | None,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> int:
    """Цена одного завершённого хода на этой модели — ЕДИНСТВЕННЫЙ мост цены чата.

    Тот же вызов питает pre-generation balance-гейт, финальное идемпотентное списание и
    `creditCost` пользовательского контракта: режим не может быть допущен по одной цене и
    списан по другой. Аргументом моста стала модель, а не режим (решение владельца №1); второго
    механизма цены не появилось.

    ``model=None`` — сессия на дефолте инстанса: цена берётся у той модели, которой ход
    фактически обслуживается.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    from app.instance_config.models import instance_default_model

    resolved = (model or "").strip() or instance_default_model(settings=cfg, snapshot=snap)
    provider = cfg.credits_provider_for_model(resolved)
    overlay = _overlay_tokens(chat_tariff_id(provider, resolved), snap)
    return overlay if overlay is not None else cfg.chat_credit_cost_general


def photo_unit_credits(
    model: FalModel,
    resolution: str | None,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> int:
    """Кредиты за ОДНО изображение в этом разрешении: оверлей ячейки, иначе реестр."""
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    effective = _effective_resolution(model, resolution)
    if effective is not None:
        overlay = _overlay_tokens(photo_tariff_id(model.id, effective), snap)
        if overlay is not None:
            return overlay
    return image_unit_credits(model, resolution, base_credits=base_credits_for(model, cfg))


def _effective_resolution(model: FalModel, resolution: str | None) -> str | None:
    """Ключ таблицы качества, по которому реально считается цена (та же лестница, что в реестре).

    Разрешение, не присланное клиентом, подставляется сервером до тарификации; фолбэк здесь
    повторяет `image_unit_credits`, иначе оверлей накрыл бы не ту ячейку, по которой списывается.
    """
    table = model.resolution_credits
    if not table:
        return None
    if resolution is not None and resolution in table:
        return resolution
    if "1K" in table:
        return "1K"
    return next(iter(table), None)


def media_run_price(
    *,
    model: FalModel,
    num_images: int | None = None,
    duration: str | None = None,
    resolution: str | None = None,
    generate_audio: bool | None = None,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> int:
    """Полная цена одного запуска: оверлей ячейки, иначе сегодняшняя формула ``run_price()``.

    Фото: цена ячейки × ``numImages`` — множитель НАЗВАН единицей `image` и потому не спрятан.
    Видео: цена ячейки целиком — длительность, разрешение и звук развёрнуты в координаты строки,
    множителей не остаётся ни одного. Округление `ceil` в дефолте применяется ОДИН раз, к полной
    цене запуска: `ceil(23×2×1.5)=69`, а `2×ceil(23×1.5)=70` — пачечная ячейка сдвинула бы
    действующее списание на единицу.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    if model.kind == KIND_IMAGE:
        unit = photo_unit_credits(model, resolution, settings=cfg, snapshot=snap)
        return unit * max(1, num_images or 1)
    seconds = duration_seconds(duration) if duration else None
    if seconds is not None:
        audio = bool(generate_audio) and has_audio_dimension(model)
        overlay = _overlay_tokens(video_tariff_id(model.id, resolution, seconds, audio=audio), snap)
        if overlay is not None:
            return overlay
    return run_price(
        model=model,
        base_credits=base_credits_for(model, cfg),
        num_images=num_images,
        duration=duration,
        resolution=resolution,
        generate_audio=generate_audio,
    )


# --- Реестр строк для GET /v1/admin/pricing ----------------------------------------------


@dataclass(frozen=True)
class TariffRow:
    """Одна строка ответа `/pricing`: координата варианта плюс действующая цена."""

    tariff_id: str
    kind: str
    name: str | None
    tokens: int
    provider: str | None
    model: str | None
    unit: str
    options: dict[str, object] | None
    updated_at: datetime | None


def _row(
    *,
    tariff_id: str,
    kind: str,
    name: str,
    default_tokens: int,
    provider: str,
    model: str,
    unit: str,
    options: dict[str, object] | None,
    snapshot: InstanceConfigSnapshot,
) -> TariffRow:
    overlay = snapshot.tariffs.get(tariff_id)
    return TariffRow(
        tariff_id=tariff_id,
        kind=kind,
        name=name,
        tokens=overlay.tokens if overlay is not None else default_tokens,
        provider=provider,
        model=model,
        unit=unit,
        options=options,
        updated_at=overlay.updated_at if overlay is not None else None,
    )


def _video_row_name(cell: VideoCell) -> str:
    parts = [cell.model.title, f"{cell.seconds} с"]
    if cell.resolution is not None:
        parts.append(cell.resolution)
    if has_audio_dimension(cell.model):
        parts.append("со звуком" if cell.audio else "без звука")
    return " · ".join(parts)


def pricing_rows(
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> list[TariffRow]:
    """Строка на КАЖДЫЙ поддерживаемый вариант, включая ненастроенные.

    У ненастроенной строки `tokens` — действующее сегодня значение.

    Вариант, не отданный здесь, для CRM не существует, и второго, догадочного каталога
    возможностей она не заводит. Перечень chat-строк выводится из ПОЛНОГО известного каталога
    моделей, а не из витрины: модель, снятая с витрины, продолжает обслуживать уже созданные
    сессии, и строка без тарифа означала бы списание по неотображаемой цене.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    rows: list[TariffRow] = []
    seen_models: set[str] = set()
    for provider in cfg.credits_providers():
        for model_id, display_name in cfg.allowed_models_for(provider).items():
            if model_id in seen_models:
                continue
            seen_models.add(model_id)
            rows.append(
                _row(
                    tariff_id=chat_tariff_id(provider, model_id),
                    kind=KIND_CHAT,
                    name=display_name,
                    default_tokens=cfg.chat_credit_cost_general,
                    provider=provider,
                    model=model_id,
                    unit=UNIT_MESSAGE,
                    options=None,
                    snapshot=snap,
                )
            )
    if not cfg.fal_api_key.strip():
        # Тот же гейт, что у GET /v1/models: без ключа fal инстанс не умеет запустить ни одну
        # генерацию, и объявлять её тариф значило бы предложить оператору править цену
        # несуществующей возможности.
        return rows
    for model in models_of_kind(KIND_IMAGE):
        for cell in photo_cells(model):
            rows.append(
                _row(
                    tariff_id=cell.tariff_id,
                    kind=KIND_PHOTO,
                    name=f"{model.title} · {cell.resolution}",
                    default_tokens=default_photo_cell_price(cell, cfg),
                    provider=PROVIDER_FAL,
                    model=model.id,
                    unit=UNIT_IMAGE,
                    options={"resolution": cell.resolution},
                    snapshot=snap,
                )
            )
    for model in models_of_kind(KIND_VIDEO):
        for video_cell in video_cells(model):
            options: dict[str, object] = {"duration_seconds": video_cell.seconds}
            if video_cell.resolution is not None:
                options["resolution"] = video_cell.resolution
            if has_audio_dimension(model):
                options["audio"] = video_cell.audio
            rows.append(
                _row(
                    tariff_id=video_cell.tariff_id,
                    kind=KIND_VIDEO_ROW,
                    name=_video_row_name(video_cell),
                    default_tokens=default_video_cell_price(video_cell, cfg),
                    provider=PROVIDER_FAL,
                    model=model.id,
                    unit=UNIT_GENERATION,
                    options=options,
                    snapshot=snap,
                )
            )
    return rows


def find_tariff_row(
    tariff_id: str,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> TariffRow | None:
    """Строка по идентификатору, либо ``None`` — неизвестный вариант (→ `400`, строк не создаём)."""
    for row in pricing_rows(settings=settings, snapshot=snapshot):
        if row.tariff_id == tariff_id:
            return row
    return None


def media_model_of_tariff(tariff_id: str) -> FalModel | None:
    """Модель fal, к которой относится строка тарифа (для инвалидации производных значений)."""
    parts = tariff_id.split(":")
    if len(parts) < 2 or parts[0] not in (KIND_PHOTO, KIND_VIDEO_ROW):
        return None
    return find_model(parts[1])
