"""Unit: реестр тарифных строк и разрешение цены (ADR-099 §3, §4, §5).

**Несущий кейс файла — регрессионный барьер «пустой оверлей = сегодняшний день».** Дефолт КАЖДОЙ
строки сверяется с тем, что списывает действующий код (``run_price()`` / ``image_unit_credits()``
для медиа, ``CHAT_CREDIT_COST_GENERAL`` для чата). Числа из документа сюда НЕ переписываются:
константа, скопированная из ADR, проверяла бы совпадение теста с документом, а не совпадение кода
с действующим списанием — и разошлась бы молча при первой же калибровке.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.config import Settings
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    TariffOverlay,
)
from app.instance_config.tariffs import (
    KIND_CHAT,
    KIND_PHOTO,
    KIND_VIDEO_ROW,
    UNIT_GENERATION,
    UNIT_IMAGE,
    UNIT_MESSAGE,
    chat_tariff_id,
    chat_turn_credit_cost,
    find_tariff_row,
    has_audio_dimension,
    media_model_of_tariff,
    media_run_price,
    photo_cells,
    photo_tariff_id,
    photo_unit_credits,
    pricing_rows,
    video_cells,
    video_tariff_id,
)
from app.media_generation.catalog import (
    KIND_IMAGE,
    KIND_VIDEO,
    duration_seconds,
    image_unit_credits,
    models_of_kind,
    run_price,
)

_NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.UTC)


def _settings(**kwargs: Any) -> Settings:
    return Settings(
        **{
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "FAL_API_KEY": "fal-test-key",
            **kwargs,
        }
    )


def _tariff_snapshot(**tokens: int) -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        tariffs={
            tariff_id: TariffOverlay(tariff_id=tariff_id, tokens=value, updated_at=_NOW)
            for tariff_id, value in tokens.items()
        }
    )


# ============================== §3: идентификатор — функция координат ========================
def test_identifier_segments_are_normalized_and_a_missing_dimension_is_the_na_literal() -> None:
    """`na`, а не пустой сегмент: пустой сделал бы `a::b` и `a:b` неразличимыми при чтении."""
    assert chat_tariff_id("  OpenAI ", " GPT-4.1 ") == "chat:openai:gpt-4.1"
    assert photo_tariff_id("Nano-Banana-2", "1K") == "photo:nano-banana-2:1k"
    assert video_tariff_id("Veo-3.1", "1080P", 8, audio=True) == "video:veo-3.1:1080p:8:1"
    assert video_tariff_id("kling-video", None, 10, audio=False) == "video:kling-video:na:10:0"


def test_two_spellings_of_one_duration_collapse_into_one_identifier() -> None:
    """`duration_seconds()` снимает два написания одной величины ещё до сборки координаты."""
    assert duration_seconds("8s") == duration_seconds("8") == 8

    seconds = duration_seconds("8s")
    assert seconds is not None
    assert video_tariff_id("veo-3.1", "1080p", seconds, audio=False) == "video:veo-3.1:1080p:8:0"


def test_media_model_of_tariff_reads_the_coordinate_back() -> None:
    assert media_model_of_tariff("photo:nano-banana-2:1k") is not None
    assert media_model_of_tariff("video:veo-3.1:1080p:8:1") is not None
    assert media_model_of_tariff("chat:openai:gpt-4.1") is None
    assert media_model_of_tariff("photo:no-such-model:1k") is None
    assert media_model_of_tariff("garbage") is None


# ============================== §4: пустой оверлей = сегодняшний день =======================
def _photo_row_ids(settings: Settings) -> dict[str, Any]:
    return {
        row.tariff_id: row
        for row in pricing_rows(settings=settings, snapshot=EMPTY_SNAPSHOT)
        if row.kind == KIND_PHOTO
    }


def _video_row_ids(settings: Settings) -> dict[str, Any]:
    return {
        row.tariff_id: row
        for row in pricing_rows(settings=settings, snapshot=EMPTY_SNAPSHOT)
        if row.kind == KIND_VIDEO_ROW
    }


@pytest.mark.parametrize("model", models_of_kind(KIND_IMAGE), ids=lambda m: m.id)
def test_every_photo_row_default_equals_todays_charge(model: Any) -> None:
    """Строка на пару «модель × разрешение», единица `image`, цена ОДНОГО изображения.

    Сверка двусторонняя: (а) с формулой, действовавшей ДО выката (`image_unit_credits`), и (б) с
    тем, что фактически спишет медиа-сабмит СЕЙЧАС (`media_run_price` на пустом снимке). Первая
    доказывает «ничего не изменилось», вторая — «строка не разошлась со списанием».
    """
    settings = _settings()
    rows = _photo_row_ids(settings)

    for cell in photo_cells(model):
        row = rows[cell.tariff_id]
        today = image_unit_credits(model, cell.resolution, base_credits=model.default_credits)
        charged_now = media_run_price(
            model=model,
            num_images=1,
            resolution=cell.resolution,
            settings=settings,
            snapshot=EMPTY_SNAPSHOT,
        )
        assert row.tokens == today
        assert row.tokens == charged_now
        assert row.unit == UNIT_IMAGE
        assert row.options == {"resolution": cell.resolution}


@pytest.mark.parametrize("model", models_of_kind(KIND_VIDEO), ids=lambda m: m.id)
def test_every_video_row_default_equals_todays_charge(model: Any) -> None:
    """Строка на КАЖДУЮ комбинацию, единица `generation` — полная цена одного запуска.

    ⚠️ Ровно поэтому цена развёрнута до ДЛИТЕЛЬНОСТИ, а не до «цены одной пачки»: `ceil` в
    сегодняшней формуле применяется ОДИН раз к полной цене запуска, и пачечная ячейка сдвинула бы
    действующее списание на единицу.
    """
    settings = _settings()
    rows = _video_row_ids(settings)

    for cell in video_cells(model):
        row = rows[cell.tariff_id]
        today = run_price(
            model=model,
            base_credits=model.default_credits,
            duration=cell.duration,
            resolution=cell.resolution,
            generate_audio=cell.audio,
        )
        charged_now = media_run_price(
            model=model,
            duration=cell.duration,
            resolution=cell.resolution,
            generate_audio=cell.audio,
            settings=settings,
            snapshot=EMPTY_SNAPSHOT,
        )
        assert row.tokens == today
        assert row.tokens == charged_now
        assert row.unit == UNIT_GENERATION
        assert row.options["duration_seconds"] == cell.seconds


def test_one_time_ceiling_is_not_replaced_by_a_per_pack_one() -> None:
    """Diff по §4.2: пачечная ячейка дала бы 70 вместо 69 — сдвиг действующего списания.

    Числа не переписаны из документа: обе стороны ВЫЧИСЛЯЮТСЯ из реестра, и кейс падает ровно
    тогда, когда цена запуска перестаёт быть одним `ceil` от полного произведения.
    """
    import math

    model = next(m for m in models_of_kind(KIND_VIDEO) if has_audio_dimension(m))
    assert model.audio_multiplier is not None
    base = model.default_credits
    duration = next(
        d
        for d in model.text_variant.durations
        if (seconds := duration_seconds(d)) is not None
        and model.base_duration_seconds is not None
        and seconds > model.base_duration_seconds
    )
    seconds = duration_seconds(duration)
    assert seconds is not None and model.base_duration_seconds is not None
    packs = math.ceil(seconds / model.base_duration_seconds)

    once = math.ceil(base * packs * model.audio_multiplier)
    per_pack = packs * math.ceil(base * model.audio_multiplier)

    charged = run_price(
        model=model, base_credits=base, duration=duration, resolution=None, generate_audio=True
    )
    assert charged == once
    assert once != per_pack  # предусловие: разница реальна, кейс не вырожден


def test_chat_row_default_is_the_general_price_of_this_instance() -> None:
    """§4.3: дефолт строки чата = `CHAT_CREDIT_COST_GENERAL`, а не литерал и не надбавка режима."""
    settings = _settings(
        CHAT_CREDIT_COST_GENERAL=7,
        CHAT_CREDIT_COST_RESEARCH=11,
        CHAT_CREDIT_COST_STUDY_LEARN=13,
    )

    chat_rows = [
        row
        for row in pricing_rows(settings=settings, snapshot=EMPTY_SNAPSHOT)
        if row.kind == KIND_CHAT
    ]

    assert chat_rows
    for row in chat_rows:
        assert row.tokens == settings.chat_credit_cost_general == 7
        assert row.unit == UNIT_MESSAGE
        assert row.options is None
        assert row.model is not None
        assert row.tokens == chat_turn_credit_cost(
            row.model, settings=settings, snapshot=EMPTY_SNAPSHOT
        )


def test_no_row_carries_a_version_stamp_until_it_is_edited() -> None:
    """`updated_at != null` — единственный видимый признак того, что величину трогали из CRM."""
    rows = pricing_rows(settings=_settings(), snapshot=EMPTY_SNAPSHOT)

    assert rows
    assert all(row.updated_at is None for row in rows)


# ============================== перечень строк = перечень запусков ==========================
def test_there_is_a_row_for_every_combination_the_instance_can_actually_run() -> None:
    """Вариант, не отданный в `/pricing`, для CRM не существует — второго каталога она не заводит.

    Перечень строится ИЗ РЕЕСТРА возможностей, а не из числа в документе: кейс падает и когда
    строка пропала, и когда появилась лишняя.
    """
    settings = _settings()
    expected: set[str] = set()
    for model in models_of_kind(KIND_IMAGE):
        expected |= {cell.tariff_id for cell in photo_cells(model)}
    for model in models_of_kind(KIND_VIDEO):
        expected |= {cell.tariff_id for cell in video_cells(model)}

    media_rows = {
        row.tariff_id
        for row in pricing_rows(settings=settings, snapshot=EMPTY_SNAPSHOT)
        if row.kind in (KIND_PHOTO, KIND_VIDEO_ROW)
    }

    assert media_rows == expected


def test_chat_rows_come_from_the_full_catalog_not_from_the_shop_window() -> None:
    """§4.3 ⚠️: модель, снятая с витрины, продолжает обслуживать сессии и списывать деньги.

    Строка без тарифа означала бы списание по неотображаемой цене, поэтому перечень выводится из
    ПОЛНОГО известного каталога, а не из `chat.models_offered`.
    """
    from app.instance_config.settings_registry import SETTING_CHAT_MODELS_OFFERED
    from app.instance_config.snapshot import SettingOverlay

    settings = _settings()
    union = settings.allowed_models_union()
    assert len(union) > 1  # предусловие: есть что снимать с витрины
    kept = [next(iter(union))]
    narrow = InstanceConfigSnapshot(
        settings={
            SETTING_CHAT_MODELS_OFFERED: SettingOverlay(
                setting_id=SETTING_CHAT_MODELS_OFFERED, value=kept, updated_at=_NOW
            )
        }
    )

    chat_rows = {
        row.model
        for row in pricing_rows(settings=settings, snapshot=narrow)
        if row.kind == KIND_CHAT
    }

    assert chat_rows == set(union)


def test_a_model_dropped_from_the_window_keeps_its_price_resolvable() -> None:
    from app.instance_config.settings_registry import SETTING_CHAT_MODELS_OFFERED
    from app.instance_config.snapshot import SettingOverlay

    settings = _settings(CHAT_CREDIT_COST_GENERAL=5)
    union = list(settings.allowed_models_union())
    dropped = union[-1]
    narrow = InstanceConfigSnapshot(
        settings={
            SETTING_CHAT_MODELS_OFFERED: SettingOverlay(
                setting_id=SETTING_CHAT_MODELS_OFFERED, value=union[:1], updated_at=_NOW
            )
        }
    )

    assert chat_turn_credit_cost(dropped, settings=settings, snapshot=narrow) == 5


def test_without_a_fal_key_the_instance_declares_no_media_tariff_at_all() -> None:
    """Тот же гейт, что у `GET /v1/models`: править цену несуществующей возможности незачем."""
    rows = pricing_rows(settings=_settings(FAL_API_KEY="  "), snapshot=EMPTY_SNAPSHOT)

    assert rows
    assert {row.kind for row in rows} == {KIND_CHAT}


# ============================== оверлей побеждает дефолт ====================================
def test_chat_overlay_wins_over_the_env_default_and_is_per_model() -> None:
    """Две модели с разной ценой → разные списания; надбавки за режим больше нет вовсе."""
    settings = _settings(CHAT_CREDIT_COST_GENERAL=1)
    models = list(settings.allowed_models_union())
    assert len(models) >= 2
    premium, basic = models[0], models[1]
    provider = settings.credits_provider_for_model(premium)
    snapshot = _tariff_snapshot(**{chat_tariff_id(provider, premium): 9})

    assert chat_turn_credit_cost(premium, settings=settings, snapshot=snapshot) == 9
    assert chat_turn_credit_cost(basic, settings=settings, snapshot=snapshot) == 1


def test_a_session_without_a_model_is_priced_by_the_instance_default_model() -> None:
    """`model=None` — сессия на дефолте: цена берётся у той модели, которой ход и обслуживается."""
    settings = _settings(CHAT_CREDIT_COST_GENERAL=1)
    default_id = settings.default_model()
    provider = settings.credits_provider_for_model(default_id)
    snapshot = _tariff_snapshot(**{chat_tariff_id(provider, default_id): 6})

    assert chat_turn_credit_cost(None, settings=settings, snapshot=snapshot) == 6
    assert chat_turn_credit_cost("", settings=settings, snapshot=snapshot) == 6


@pytest.mark.parametrize("model", models_of_kind(KIND_IMAGE), ids=lambda m: m.id)
def test_photo_overlay_applies_to_the_cell_that_is_actually_charged(model: Any) -> None:
    """Оверлей обязан накрыть ту ячейку, по которой считается цена, а не «похожую».

    Разрешение, не присланное клиентом, подставляется сервером ДО тарификации: фолбэк здесь
    повторяет `image_unit_credits`, иначе правка оператора не применилась бы к дефолтному запуску.
    """
    settings = _settings()
    for cell in photo_cells(model):
        snapshot = _tariff_snapshot(**{cell.tariff_id: 77})
        assert (
            photo_unit_credits(model, cell.resolution, settings=settings, snapshot=snapshot) == 77
        )
        # …и множитель `numImages` НАЗВАН единицей `image`, поэтому он не спрятан.
        assert (
            media_run_price(
                model=model,
                num_images=3,
                resolution=cell.resolution,
                settings=settings,
                snapshot=snapshot,
            )
            == 77 * 3
        )


def test_photo_overlay_covers_the_server_side_resolution_fallback() -> None:
    model = models_of_kind(KIND_IMAGE)[0]
    settings = _settings()
    fallback_resolution = (
        "1K" if "1K" in model.resolution_credits else next(iter(model.resolution_credits))
    )
    snapshot = _tariff_snapshot(**{photo_tariff_id(model.id, fallback_resolution): 42})

    assert photo_unit_credits(model, None, settings=settings, snapshot=snapshot) == 42


@pytest.mark.parametrize("model", models_of_kind(KIND_VIDEO), ids=lambda m: m.id)
def test_video_overlay_replaces_the_whole_run_price_of_that_combination(model: Any) -> None:
    settings = _settings()
    cell = video_cells(model)[0]
    snapshot = _tariff_snapshot(**{cell.tariff_id: 55})

    assert (
        media_run_price(
            model=model,
            duration=cell.duration,
            resolution=cell.resolution,
            generate_audio=cell.audio,
            settings=settings,
            snapshot=snapshot,
        )
        == 55
    )


def test_an_orphan_overlay_row_does_not_take_part_in_resolution() -> None:
    """Строка-сирота безопасна.

    Она переживает временное исчезновение варианта, но в разрешении НЕ участвует.
    """
    settings = _settings()
    snapshot = _tariff_snapshot(**{"video:model-that-was-removed:1080p:8:1": 999})

    rows = pricing_rows(settings=settings, snapshot=snapshot)

    assert "video:model-that-was-removed:1080p:8:1" not in {row.tariff_id for row in rows}
    assert (
        find_tariff_row(
            "video:model-that-was-removed:1080p:8:1", settings=settings, snapshot=snapshot
        )
        is None
    )


def test_operator_base_credits_scale_the_default_of_every_cell_of_that_model() -> None:
    """`MEDIA_MODEL_CREDITS` остаётся домом БАЗОВОЙ цены; оверлей строк её не отменяет."""
    model = models_of_kind(KIND_VIDEO)[0]
    scaled = model.default_credits * 2
    settings = _settings(MEDIA_MODEL_CREDITS=f'{{"{model.id}": {scaled}}}')

    rows = {row.tariff_id: row for row in pricing_rows(settings=settings, snapshot=EMPTY_SNAPSHOT)}

    for cell in video_cells(model):
        expected = run_price(
            model=model,
            base_credits=scaled,
            duration=cell.duration,
            resolution=cell.resolution,
            generate_audio=cell.audio,
        )
        assert rows[cell.tariff_id].tokens == expected


def test_a_model_that_does_not_price_audio_has_no_audio_coordinate() -> None:
    """Строка, отличающаяся только флагом и совпадающая ценой, — удвоение каталога без решения."""
    silent = [m for m in models_of_kind(KIND_VIDEO) if not has_audio_dimension(m)]
    assert silent  # предусловие: такая модель в реестре есть

    for model in silent:
        assert all(cell.audio is False for cell in video_cells(model))
        for cell in video_cells(model):
            assert "audio" not in (
                find_tariff_row(
                    cell.tariff_id, settings=_settings(), snapshot=EMPTY_SNAPSHOT
                ).options
                or {}
            )


def test_find_tariff_row_returns_none_for_an_unknown_identifier() -> None:
    assert find_tariff_row("chat:openai:no-such-model", settings=_settings()) is None
