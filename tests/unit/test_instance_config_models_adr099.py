"""Unit: витрина chat-моделей инстанса (ADR-099 §8.1, амендмент к ADR-076).

**Витрина и тариф — разные вопросы.** ``chat.models_offered`` гейтит КАТАЛОГ, а не бэкенд: уже
созданная сессия на снятой модели продолжает работать и тарифицироваться (это проверяется в
``test_instance_config_tariffs_adr099.py``). Связывать витрину с ценой нельзя ни в ту, ни в
другую сторону.

**Инвариант «дефолт входит в витрину» держат ДВА барьера, и это разные барьеры.** Первый —
контракт настройки (`400` на нарушающую правку) — живёт в интеграционных кейсах ручки. Второй —
здесь: код принудительно включает дефолт в каталог независимо от корректности оверлея.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.config import Settings
from app.instance_config.models import (
    catalog_rows,
    instance_default_model,
    model_is_selectable,
    offered_model_ids,
)
from app.instance_config.settings_registry import (
    SETTING_CHAT_DEFAULT_MODEL,
    SETTING_CHAT_MODELS_OFFERED,
)
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    SettingOverlay,
)

_NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.UTC)


def _settings(**kwargs: Any) -> Settings:
    return Settings(**{"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-openai-test", **kwargs})


def _snapshot(**overlays: Any) -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        settings={
            setting_id: SettingOverlay(setting_id=setting_id, value=value, updated_at=_NOW)
            for setting_id, value in overlays.items()
        }
    )


# ============================== дефолт по умолчанию =========================================
def test_the_default_is_the_env_one_until_the_operator_changes_it() -> None:
    settings = _settings()

    assert instance_default_model(settings=settings, snapshot=EMPTY_SNAPSHOT) == (
        settings.default_model()
    )


def test_the_overlay_changes_the_default_model() -> None:
    settings = _settings()
    other = next(
        model_id
        for model_id in settings.allowed_models_union()
        if model_id != settings.default_model()
    )

    assert (
        instance_default_model(
            settings=settings, snapshot=_snapshot(**{SETTING_CHAT_DEFAULT_MODEL: other})
        )
        == other
    )


def test_an_overlay_on_a_model_the_instance_cannot_serve_is_not_applied() -> None:
    """Строка-сирота переживает исчезновение варианта, но не имеет права направить туда ход.

    Иначе провайдер, выключенный на инстансе, получал бы каждый ход и отвечал `5xx`.
    """
    settings = _settings()

    resolved = instance_default_model(
        settings=settings,
        snapshot=_snapshot(**{SETTING_CHAT_DEFAULT_MODEL: "model-that-was-removed"}),
    )

    assert resolved == settings.default_model()


# ============================== витрина =====================================================
def test_with_an_empty_overlay_the_catalog_reproduces_adr076_bit_for_bit() -> None:
    """Дефолт (все модели включённых провайдеров предложены) воспроизводит ADR-076 поэлементно."""
    settings = _settings()

    assert catalog_rows(settings=settings, snapshot=EMPTY_SNAPSHOT) == settings.catalog_models()
    assert set(offered_model_ids(settings=settings, snapshot=EMPTY_SNAPSHOT)) == set(
        settings.allowed_models_union()
    )


def test_the_operator_can_hide_a_model_and_that_is_an_amendment_to_adr076() -> None:
    """ADR-076 защищался от того, чтобы каталог УСОХ молча из-за забытой env-карты.

    Здесь снятие модели — явное, аудируемое действие с видимым следом, поэтому расширение
    осознанное, а не побочный эффект.
    """
    settings = _settings()
    union = list(settings.allowed_models_union())
    assert len(union) > 1
    default_id = settings.default_model()
    kept = [default_id] + [m for m in union if m != default_id][:1]
    dropped = [m for m in union if m not in kept]
    assert dropped  # предусловие: есть что снимать

    offered = offered_model_ids(
        settings=settings, snapshot=_snapshot(**{SETTING_CHAT_MODELS_OFFERED: kept})
    )

    assert set(offered) == set(kept)
    for model_id in dropped:
        assert (
            model_is_selectable(
                model_id,
                settings=settings,
                snapshot=_snapshot(**{SETTING_CHAT_MODELS_OFFERED: kept}),
            )
            is False
        )


def test_the_default_is_always_first_and_the_only_default_row() -> None:
    settings = _settings()
    union = list(settings.allowed_models_union())
    default_id = settings.default_model()

    rows = catalog_rows(
        settings=settings, snapshot=_snapshot(**{SETTING_CHAT_MODELS_OFFERED: union})
    )

    assert rows[0][0] == default_id
    assert [row[2] for row in rows] == [True] + [False] * (len(rows) - 1)


def test_the_code_barrier_keeps_the_default_in_the_catalog_even_if_the_overlay_drops_it() -> None:
    """ВТОРОЙ барьер — страховка кода, не зависящая от корректности оверлея.

    Первый барьер (`400` на такую правку) живёт в контракте настройки: молчаливо игнорировать
    осмысленное действие оператора нельзя, поэтому у инварианта ДВА разных держателя, а не дубль.
    """
    settings = _settings()
    default_id = settings.default_model()
    without_default = [m for m in settings.allowed_models_union() if m != default_id]
    assert without_default

    offered = offered_model_ids(
        settings=settings,
        snapshot=_snapshot(**{SETTING_CHAT_MODELS_OFFERED: without_default}),
    )

    assert offered[0] == default_id
    assert model_is_selectable(
        default_id,
        settings=settings,
        snapshot=_snapshot(**{SETTING_CHAT_MODELS_OFFERED: without_default}),
    )


def test_an_overlay_naming_an_unknown_model_is_dropped_from_the_window() -> None:
    settings = _settings()
    default_id = settings.default_model()

    offered = offered_model_ids(
        settings=settings,
        snapshot=_snapshot(**{SETTING_CHAT_MODELS_OFFERED: [default_id, "model-that-was-removed"]}),
    )

    assert offered == (default_id,)


@pytest.mark.parametrize("declared_default", [True, False])
def test_catalog_rows_carry_display_name_and_provider_for_every_offered_model(
    declared_default: bool,
) -> None:
    settings = _settings()
    union = settings.allowed_models_union()
    snapshot = (
        _snapshot(**{SETTING_CHAT_DEFAULT_MODEL: next(iter(union))})
        if declared_default
        else EMPTY_SNAPSHOT
    )

    rows = catalog_rows(settings=settings, snapshot=snapshot)

    assert rows
    for model_id, display, _is_default, provider in rows:
        assert model_id in union
        assert display
        assert provider in settings.credits_providers()
