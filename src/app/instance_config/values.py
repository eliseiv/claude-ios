"""Точки применения 14 продуктовых настроек (ADR-099 §8).

Каждая функция здесь — ЕДИНСТВЕННЫЙ способ прочитать свою величину на рабочем пути. Прямое
чтение соответствующего поля ``Settings`` в потребителе означало бы, что настройка объявлена
оператору, но им не управляется: то самое «объявлено ≠ подключено», ради которого у каждой
строки §8 назван consumer.

Порядок разрешения — один для всех: **оверлей → env → дефолт кода**.
"""

from __future__ import annotations

from app.config import Settings, get_settings, parse_moderation_block_categories
from app.instance_config.settings_registry import (
    SETTING_CATALOG_PRESETS_LOCALE,
    SETTING_CHAT_ADVERTISED_MODES,
    SETTING_CHAT_CHARACTERS_ENABLED,
    SETTING_CHAT_CODE_TOOLS_ENABLED,
    SETTING_CHAT_DISABLED_TOOL_FAMILIES,
    SETTING_CHAT_MEDIA_TOOLS_ENABLED,
    SETTING_CHAT_MEMORY_ENABLED,
    SETTING_CHAT_REASONING_LEVEL,
    SETTING_CHAT_THINKING_DISPLAY,
    SETTING_CHAT_VOICE_INPUT_ENABLED,
    SETTING_MODERATION_BLOCK_CATEGORIES,
    SETTING_MODERATION_ENABLED,
    find_setting,
    resolve_setting,
)
from app.instance_config.snapshot import InstanceConfigSnapshot


def _bool(
    setting_id: str, settings: Settings | None, snapshot: InstanceConfigSnapshot | None
) -> bool:
    value = resolve_setting(setting_id, settings=settings, snapshot=snapshot)
    return bool(value)


def characters_enabled(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> bool:
    return _bool(SETTING_CHAT_CHARACTERS_ENABLED, settings, snapshot)


def memory_enabled(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> bool:
    return _bool(SETTING_CHAT_MEMORY_ENABLED, settings, snapshot)


def voice_input_enabled(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> bool:
    return _bool(SETTING_CHAT_VOICE_INPUT_ENABLED, settings, snapshot)


def code_tools_enabled(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> bool:
    return _bool(SETTING_CHAT_CODE_TOOLS_ENABLED, settings, snapshot)


def media_tools_enabled(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> bool:
    return _bool(SETTING_CHAT_MEDIA_TOOLS_ENABLED, settings, snapshot)


def moderation_enabled(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> bool:
    return _bool(SETTING_MODERATION_ENABLED, settings, snapshot)


def disabled_tool_families(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> frozenset[str]:
    value = resolve_setting(
        SETTING_CHAT_DISABLED_TOOL_FAMILIES, settings=settings, snapshot=snapshot
    )
    return frozenset(value)


def advertised_generation_modes(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> tuple[str, ...]:
    """Объявляемые режимы В КАНОНИЧЕСКОМ порядке, а не в том, в каком их выбрал оператор."""
    from app.schemas.chat import DEFAULT_GENERATION_MODE, GENERATION_MODE_ORDER

    selected = set(
        resolve_setting(SETTING_CHAT_ADVERTISED_MODES, settings=settings, snapshot=snapshot)
    )
    # ADR-065 §1: `defaultGenerationMode` обязан присутствовать в списке — иначе у выпущенной
    # сборки переключатель остаётся без значения по умолчанию. Это барьер КОДА, защищающий
    # пользовательский контракт, а не молчаливое расширение выбора оператора: снять с витрины
    # можно любой режим, кроме объявленного контрактом дефолтным.
    selected.add(DEFAULT_GENERATION_MODE)
    return tuple(mode for mode in GENERATION_MODE_ORDER if mode in selected)


def reasoning_level(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> str:
    value = resolve_setting(SETTING_CHAT_REASONING_LEVEL, settings=settings, snapshot=snapshot)
    return str(value)


def anthropic_thinking_display(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> str:
    """Строка объявляется только на Anthropic-инстансах; на прочих читается env-значение."""
    cfg = settings or get_settings()
    if find_setting(SETTING_CHAT_THINKING_DISPLAY, cfg) is None:
        return cfg.resolved_anthropic_thinking_display()
    value = resolve_setting(SETTING_CHAT_THINKING_DISPLAY, settings=cfg, snapshot=snapshot)
    return str(value)


def presets_default_locale(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> str:
    value = resolve_setting(SETTING_CATALOG_PRESETS_LOCALE, settings=settings, snapshot=snapshot)
    return str(value)


def moderation_block_categories(
    *, settings: Settings | None = None, snapshot: InstanceConfigSnapshot | None = None
) -> frozenset[str]:
    """BLOCK-набор категорий из операторского CSV.

    ПОЛ БЕЗОПАСНОСТИ ДЕРЖИТ КОД, А НЕ ЗНАЧЕНИЕ: ``sexual/minors`` добавляется ПОСЛЕ разбора,
    поэтому пустая строка НЕ эквивалентна выключенной модерации — она даёт минимальный
    блок-набор. Именно это делает величину пригодной для операторской поверхности.
    """
    raw = resolve_setting(SETTING_MODERATION_BLOCK_CATEGORIES, settings=settings, snapshot=snapshot)
    return parse_moderation_block_categories(str(raw))
