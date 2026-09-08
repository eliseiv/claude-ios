"""Реестр продуктовых настроек инстанса (ADR-099 §8).

Поверхность **самоописываема**: CRM не знает ни имён, ни типов, ни допустимых значений — всё
приходит в ответе ``GET /v1/admin/settings``. Отсюда два несущих правила этого модуля:

1. **``options`` выводятся из ЕДИНСТВЕННОГО объявления в коде.** Перечень, переписанный сюда
   руками, был бы вторым домом факта и протух бы молча: значение вне перечня показывается как
   есть, но выбрать его обратно нечем — настройка становится невозвратимой.
2. **Состав зависит от инстанса.** Строка, у которой на этом инстансе нет потребителя
   (``chat.anthropic_thinking_display`` на OpenAI-инстансе), НЕ объявляется: ручка, которая
   ничего не делает, — то же мёртвое объявление, что метрика без эмиссии.

Что в поверхность НЕ входит и почему — ADR-099 §8.2 (свип по всем переменным сервиса).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.config import (
    SUPPORTED_ANTHROPIC_THINKING_DISPLAYS,
    SUPPORTED_REASONING_LEVELS,
    Settings,
    get_settings,
)

TYPE_BOOL = "bool"
TYPE_ENUM = "enum"
TYPE_MULTI_ENUM = "multi_enum"
TYPE_STRING = "string"

# Верхняя граница CSV-перечня категорий модерации. Словарь категорий принадлежит провайдеру и
# меняется без нашего релиза, поэтому ограничение стоит на ДЛИНЕ СТРОКИ, а не на перечне: оно
# защищает форму ввода и колонку JSONB, не решая за провайдера, какие категории существуют.
BLOCK_CATEGORIES_MAX_LENGTH = 512

# Значения `limits` в GET /v1/admin/capabilities — runtime-величины ЭТОГО инстанса, а не
# константы контракта (заморожены только имена ключей и типы).
PRODUCT_TOKENS_MAX = 1_000_000
TARIFF_TOKENS_MAX = 100_000
# Кошелёк целочисленный (ledger_transactions.amount BIGINT): дробную цену нам слать нечего.
TARIFF_DECIMAL_PLACES = 0


class SettingValueError(ValueError):
    """Значение нарушает НАШЕ СОБСТВЕННОЕ объявление строки (тип / options / constraints).

    Отсюда код `422`, а не `400`: CRM могла отклонить такой ввод в форме по тому, что мы ей сами
    отдали. `400` остаётся за правилом, которого в объявлении нет и быть не может.

    ``constraint`` называет НАРУШЕННЫЙ КЛЮЧ ``constraints`` (`min_items` / `max_items` /
    `max_length`) либо ``None``, если нарушен тип или перечень ``options``. Поле существует
    ровно затем, чтобы лейбл метрики отказа вычислялся из НАБЛЮДАЕМОГО ФАКТА ветки, а не из
    суждения вызывающего: нарушение размерной границы и нарушение типа — разные исходы, и
    метить их одним значением значило бы посылать дежурного не туда. ⚠️ На код ответа это НЕ
    влияет: обе ветки остаются `422` — код отвечает на «могла ли CRM отклонить это в форме»,
    лейбл на «что именно оператор нарушил».
    """

    def __init__(self, message: str, *, constraint: str | None = None) -> None:
        super().__init__(message)
        self.constraint = constraint


@dataclass(frozen=True)
class SettingSpec:
    """Объявление одной настройки: то, что уходит в контракт, плюс её дом значения."""

    setting_id: str
    type: str
    label: str
    group: str
    description: str
    env_value: Callable[[Settings], Any]
    options: Callable[[Settings], tuple[tuple[str, str], ...]] | None = None
    constraints: Mapping[str, int] | None = None
    available: Callable[[Settings], bool] = lambda _settings: True


def _model_options(settings: Settings) -> tuple[tuple[str, str], ...]:
    """Модели ВКЛЮЧЁННЫХ провайдеров — один источник для обеих модельных строк.

    Не «все известные модели»: на OpenAI-инстансе восемь Anthropic-моделей без ключа дали бы
    `5xx` на каждом ходе — выбор, который гарантированно ломает инстанс.
    """
    return tuple(settings.allowed_models_union().items())


def _generation_mode_options(_settings: Settings) -> tuple[tuple[str, str], ...]:
    from app.schemas.chat import GENERATION_MODE_ORDER

    return tuple((mode, mode) for mode in GENERATION_MODE_ORDER)


def _tool_family_options(_settings: Settings) -> tuple[tuple[str, str], ...]:
    from app.chat.tools import DISABLEABLE_TOOL_FAMILIES

    return tuple((family, family) for family in sorted(DISABLEABLE_TOOL_FAMILIES))


def _preset_locale_options(_settings: Settings) -> tuple[tuple[str, str], ...]:
    from app.chat.presets import SUPPORTED_PRESET_LOCALES

    return tuple((locale, locale) for locale in SUPPORTED_PRESET_LOCALES)


def _reasoning_level_options(_settings: Settings) -> tuple[tuple[str, str], ...]:
    return tuple((level, level) for level in SUPPORTED_REASONING_LEVELS)


def _thinking_display_options(_settings: Settings) -> tuple[tuple[str, str], ...]:
    return tuple((value, value) for value in SUPPORTED_ANTHROPIC_THINKING_DISPLAYS)


def _anthropic_instance(settings: Settings) -> bool:
    return "anthropic" in settings.credits_providers()


SETTING_CHAT_DEFAULT_MODEL = "chat.default_model"
SETTING_CHAT_MODELS_OFFERED = "chat.models_offered"
SETTING_CHAT_ADVERTISED_MODES = "chat.advertised_generation_modes"
SETTING_CHAT_REASONING_LEVEL = "chat.reasoning_level"
SETTING_CHAT_THINKING_DISPLAY = "chat.anthropic_thinking_display"
SETTING_CHAT_CHARACTERS_ENABLED = "chat.characters_enabled"
SETTING_CHAT_MEMORY_ENABLED = "chat.memory_enabled"
SETTING_CHAT_VOICE_INPUT_ENABLED = "chat.voice_input_enabled"
SETTING_CHAT_CODE_TOOLS_ENABLED = "chat.code_tools_enabled"
SETTING_CHAT_MEDIA_TOOLS_ENABLED = "chat.media_tools_enabled"
SETTING_CHAT_DISABLED_TOOL_FAMILIES = "chat.disabled_tool_families"
SETTING_MODERATION_ENABLED = "moderation.enabled"
SETTING_MODERATION_BLOCK_CATEGORIES = "moderation.block_categories"
SETTING_CATALOG_PRESETS_LOCALE = "catalog.presets_default_locale"


_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        setting_id=SETTING_CHAT_DEFAULT_MODEL,
        type=TYPE_ENUM,
        label="Модель по умолчанию",
        group="Чат",
        description=(
            "Модель, на которой идёт диалог, пока пользователь не выбрал другую. "
            "Обязана входить в список предлагаемых моделей."
        ),
        env_value=lambda s: s.default_model(),
        options=_model_options,
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_MODELS_OFFERED,
        type=TYPE_MULTI_ENUM,
        label="Предлагаемые модели",
        group="Чат",
        description=(
            "Модели, которые приложение показывает в селекторе. Снятая модель продолжает "
            "обслуживать уже созданные диалоги и тарифицируется как прежде."
        ),
        env_value=lambda s: list(s.allowed_models_union()),
        options=_model_options,
        constraints={"min_items": 1},
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_ADVERTISED_MODES,
        type=TYPE_MULTI_ENUM,
        label="Объявляемые режимы генерации",
        group="Чат",
        description=(
            "Режимы, которые приложение показывает в переключателе. `general` присутствует "
            "всегда — он режим по умолчанию, и снять его с витрины нельзя. Это объявление, а не "
            "поведение: не объявленный режим сервер по-прежнему принимает."
        ),
        env_value=lambda s: list(s.advertised_generation_modes()),
        options=_generation_mode_options,
        constraints={"min_items": 1},
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_REASONING_LEVEL,
        type=TYPE_ENUM,
        label="Глубина рассуждения",
        group="Чат",
        description="Усилие модели в режиме рассуждения.",
        env_value=lambda s: s.resolved_reasoning_level(),
        options=_reasoning_level_options,
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_THINKING_DISPLAY,
        type=TYPE_ENUM,
        label="Показ хода рассуждения",
        group="Чат",
        description="Отдавать ли приложению краткое изложение размышлений модели.",
        env_value=lambda s: s.resolved_anthropic_thinking_display(),
        options=_thinking_display_options,
        available=_anthropic_instance,
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_CHARACTERS_ENABLED,
        type=TYPE_BOOL,
        label="Выбор персонажа",
        group="Чат",
        description=(
            "Показывать каталог персонажей, принимать выбор собеседника при создании чата и "
            "озвучивать ответ голосом персонажа."
        ),
        env_value=lambda s: s.characters_enabled,
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_MEMORY_ENABLED,
        type=TYPE_BOOL,
        label="Память между чатами",
        group="Чат",
        description="Поиск по истории и явные факты пользователя.",
        env_value=lambda s: s.memory_enabled,
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_VOICE_INPUT_ENABLED,
        type=TYPE_BOOL,
        label="Голосовой ввод",
        group="Чат",
        description="Принимать аудио-вложение в сообщении пользователя.",
        env_value=lambda s: s.voice_input_enabled,
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_CODE_TOOLS_ENABLED,
        type=TYPE_BOOL,
        label="Инструменты кода",
        group="Чат",
        description="Предлагать модели инструменты работы с файлами в режиме кода.",
        env_value=lambda s: s.code_tools_enabled,
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_MEDIA_TOOLS_ENABLED,
        type=TYPE_BOOL,
        label="Генерация медиа из чата",
        group="Чат",
        description="Предлагать модели инструменты генерации фото и видео прямо в диалоге.",
        env_value=lambda s: s.chat_media_tools_enabled,
    ),
    SettingSpec(
        setting_id=SETTING_CHAT_DISABLED_TOOL_FAMILIES,
        type=TYPE_MULTI_ENUM,
        label="Отключённые семейства инструментов",
        group="Чат",
        description=(
            "Семейства, которые не предлагаются модели и не показываются в каталоге "
            "инструментов приложения."
        ),
        env_value=lambda s: sorted(s.disabled_tool_families()),
        options=_tool_family_options,
    ),
    SettingSpec(
        setting_id=SETTING_MODERATION_ENABLED,
        type=TYPE_BOOL,
        label="Модерация контента",
        group="Модерация",
        description="Проверка запроса и результата генерации перед выдачей пользователю.",
        env_value=lambda s: s.moderation_enabled,
    ),
    SettingSpec(
        setting_id=SETTING_MODERATION_BLOCK_CATEGORIES,
        type=TYPE_STRING,
        label="Блокируемые категории",
        group="Модерация",
        description=(
            "Категории провайдера модерации через запятую. Минимальный набор запретов держит "
            "сервер независимо от этого значения: пустая строка не отключает модерацию."
        ),
        env_value=lambda s: s.moderation_block_categories_raw,
        constraints={"max_length": BLOCK_CATEGORIES_MAX_LENGTH},
    ),
    SettingSpec(
        setting_id=SETTING_CATALOG_PRESETS_LOCALE,
        type=TYPE_ENUM,
        label="Язык каталогов по умолчанию",
        group="Каталог",
        description=(
            "Язык каталогов пресетов, персонажей и голосов, когда клиент не прислал свой. Тот "
            "же язык уходит в сегментацию пейволла."
        ),
        env_value=lambda s: s.resolved_presets_default_locale(),
        options=_preset_locale_options,
    ),
)

_BY_ID: dict[str, SettingSpec] = {spec.setting_id: spec for spec in _SPECS}


def declared_settings(settings: Settings | None = None) -> tuple[SettingSpec, ...]:
    """Строки, объявляемые ЭТИМ инстансом, в порядке отображения."""
    cfg = settings or get_settings()
    return tuple(spec for spec in _SPECS if spec.available(cfg))


def find_setting(setting_id: str, settings: Settings | None = None) -> SettingSpec | None:
    """Объявленная строка по идентификатору, либо ``None`` (→ `400`, настройка не создаётся)."""
    cfg = settings or get_settings()
    spec = _BY_ID.get(setting_id)
    if spec is None or not spec.available(cfg):
        return None
    return spec


def validate_setting_value(spec: SettingSpec, value: Any, settings: Settings) -> Any:
    """Привести и проверить значение по объявлению строки. Нарушение → ``SettingValueError``.

    Проверяется РОВНО то, что мы объявили: тип, ``options``, ``constraints``. Пустой список у
    строки с ``min_items: 1`` — нарушение объявления, а НЕ «вернуть дефолт»: пустой env значит
    «оператор ничего не сказал», пустой оверлей — «оператор явно выбрал ничего», и подмена
    второго первым выдала бы за выбор оператора конфигурацию, которой он не выбирал.
    """
    allowed = {value for value, _label in spec.options(settings)} if spec.options else set()
    constraints = spec.constraints or {}
    if spec.type == TYPE_BOOL:
        if not isinstance(value, bool):
            raise SettingValueError(f"{spec.setting_id}: expected a boolean")
        return value
    if spec.type == TYPE_ENUM:
        if not isinstance(value, str) or value not in allowed:
            raise SettingValueError(f"{spec.setting_id}: value is not one of the declared options")
        return value
    if spec.type == TYPE_MULTI_ENUM:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise SettingValueError(f"{spec.setting_id}: expected a list of strings")
        selected: list[str] = []
        for item in value:
            if item not in allowed:
                raise SettingValueError(
                    f"{spec.setting_id}: '{item}' is not one of the declared options"
                )
            if item not in selected:
                selected.append(item)
        min_items = constraints.get("min_items")
        if min_items is not None and len(selected) < min_items:
            raise SettingValueError(
                f"{spec.setting_id}: at least {min_items} value(s) required",
                constraint="min_items",
            )
        max_items = constraints.get("max_items")
        if max_items is not None and len(selected) > max_items:
            raise SettingValueError(
                f"{spec.setting_id}: at most {max_items} value(s) allowed",
                constraint="max_items",
            )
        return selected
    if not isinstance(value, str):
        raise SettingValueError(f"{spec.setting_id}: expected a string")
    max_length = constraints.get("max_length")
    if max_length is not None and len(value) > max_length:
        raise SettingValueError(
            f"{spec.setting_id}: longer than {max_length} characters",
            constraint="max_length",
        )
    return value


def coerce_stored_setting_value(setting_id: str, value: Any, settings: Settings) -> Any | None:
    """Значение из БД, приведённое к объявлению, либо ``None`` — «игнорировать эту строку».

    ``None`` возвращается и для строки-сироты (настройка снята с этого инстанса), и для
    значения не по типу: оверлей не имеет права уронить процесс, а тихо применить значение,
    которого мы не объявляли, — тем более.
    """
    spec = find_setting(setting_id, settings)
    if spec is None:
        return None
    try:
        return validate_setting_value(spec, value, settings)
    except SettingValueError:
        return None


def resolve_setting(
    setting_id: str,
    *,
    settings: Settings | None = None,
    snapshot: Any = None,
) -> Any:
    """Значение настройки по единственному порядку: **оверлей → env → дефолт кода**."""
    from app.instance_config.snapshot import get_snapshot

    cfg = settings or get_settings()
    spec = find_setting(setting_id, cfg)
    if spec is None:
        raise KeyError(setting_id)
    snap = snapshot if snapshot is not None else get_snapshot()
    overlay = snap.settings.get(setting_id)
    if overlay is not None:
        return overlay.value
    return spec.env_value(cfg)


def setting_options(spec: SettingSpec, settings: Settings) -> Sequence[tuple[str, str]] | None:
    return spec.options(settings) if spec.options else None
