"""Каталог chat-моделей инстанса с учётом операторских настроек (ADR-099 §8).

**Витрина и тариф — разные вопросы.** ``chat.models_offered`` гейтит КАТАЛОГ, а не бэкенд: уже
созданная сессия на снятой модели продолжает работать и тарифицироваться, а строка тарифа этой
модели остаётся в `/pricing`. Связывать витрину с ценой нельзя ни в ту, ни в другую сторону.

**Инвариант «дефолт входит в витрину» держат ДВА барьера, и это разные барьеры.** Первый —
контракт настройки: правка, снимающая с витрины текущую дефолтную модель, отвергается `400`.
Второй — здесь: дефолт принудительно включается в каталог и идёт первым, независимо от того,
корректен ли оверлей. Прежняя формулировка «дефолт всегда присутствует, даже если оператор снял
его с витрины» снята для ПЕРВОГО барьера: молчаливо игнорировать осмысленное действие оператора
нельзя. Второй барьер остаётся страховкой кода.
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.instance_config.settings_registry import (
    SETTING_CHAT_DEFAULT_MODEL,
    SETTING_CHAT_MODELS_OFFERED,
    resolve_setting,
)
from app.instance_config.snapshot import InstanceConfigSnapshot, get_snapshot


def instance_default_model(
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> str:
    """Модель по умолчанию: оверлей → env (`OPENAI_MODEL`/`ANTHROPIC_MODEL`) → дефолт кода.

    Оверлей на модель, которой на инстансе больше нет (провайдер выключен, модель снята из
    каталога), НЕ применяется: строка-сирота переживает временное исчезновение варианта, но не
    имеет права направить ход на модель, которую инстанс не умеет обслужить.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    value = resolve_setting(SETTING_CHAT_DEFAULT_MODEL, settings=cfg, snapshot=snap)
    if isinstance(value, str) and value in cfg.allowed_models_union():
        return value
    return cfg.default_model()


def offered_model_ids(
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> tuple[str, ...]:
    """Модели, предлагаемые витриной, в порядке отображения: дефолт первым."""
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    union = cfg.allowed_models_union()
    selected = resolve_setting(SETTING_CHAT_MODELS_OFFERED, settings=cfg, snapshot=snap)
    offered = {model_id for model_id in selected if model_id in union}
    default_id = instance_default_model(settings=cfg, snapshot=snap)
    ordered = [default_id]
    ordered.extend(model_id for model_id in union if model_id != default_id and model_id in offered)
    return tuple(ordered)


def catalog_rows(
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> list[tuple[str, str, bool, str]]:
    """Строки `GET /v1/models` для чата: ``(id, displayName, default, provider)``.

    При пустом оверлее результат поэлементно равен ``Settings.catalog_models()`` — витрина по
    умолчанию воспроизводит встроенный каталог бит-в-бит.
    """
    cfg = settings or get_settings()
    snap = snapshot if snapshot is not None else get_snapshot()
    base = {row[0]: row for row in cfg.catalog_models()}
    union = cfg.allowed_models_union()
    rows: list[tuple[str, str, bool, str]] = []
    for index, model_id in enumerate(offered_model_ids(settings=cfg, snapshot=snap)):
        known = base.get(model_id)
        display = known[1] if known is not None else union.get(model_id, model_id)
        provider = known[3] if known is not None else cfg.credits_provider_for_model(model_id)
        rows.append((model_id, display, index == 0, provider))
    return rows


def model_is_selectable(
    model_id: str,
    *,
    settings: Settings | None = None,
    snapshot: InstanceConfigSnapshot | None = None,
) -> bool:
    """Можно ли ВЫБРАТЬ модель при создании сессии.

    Проверка применяется только на создании: уже созданная сессия на снятой модели продолжает
    работать (ADR-034 фиксирует модель за сессией), и переоценивать её выбор задним числом
    значило бы сломать живой диалог правкой витрины.
    """
    return model_id in offered_model_ids(settings=settings, snapshot=snapshot)
