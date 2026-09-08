"""Unit: реестр продуктовых настроек и порядок разрешения значения (ADR-099 §2, §8, §11).

Дом сценариев — [modules/admin/09-testing.md §Настройки]. Здесь проверяется КОМПОНЕНТ: реестр,
валидация по объявлению и резолвер. Сквозная цепь «правка → пользовательская ручка» компонентным
тестом НЕ доказывается (он сам конструирует снимок) и живёт в
``tests/integration/test_admin_economics_wiring_adr099.py``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import pytest

from app.config import (
    SUPPORTED_ANTHROPIC_THINKING_DISPLAYS,
    SUPPORTED_REASONING_LEVELS,
    Settings,
)
from app.instance_config import values as instance_values
from app.instance_config.models import offered_model_ids
from app.instance_config.settings_registry import (
    BLOCK_CATEGORIES_MAX_LENGTH,
    SETTING_CATALOG_PRESETS_LOCALE,
    SETTING_CHAT_ADVERTISED_MODES,
    SETTING_CHAT_CHARACTERS_ENABLED,
    SETTING_CHAT_DEFAULT_MODEL,
    SETTING_CHAT_DISABLED_TOOL_FAMILIES,
    SETTING_CHAT_MODELS_OFFERED,
    SETTING_CHAT_REASONING_LEVEL,
    SETTING_CHAT_THINKING_DISPLAY,
    SETTING_MODERATION_BLOCK_CATEGORIES,
    SETTING_MODERATION_ENABLED,
    SettingValueError,
    coerce_stored_setting_value,
    declared_settings,
    find_setting,
    resolve_setting,
    validate_setting_value,
)
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    SettingOverlay,
    load_snapshot,
)

_NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.UTC)


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


def _snapshot(**overlays: Any) -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        settings={
            setting_id: SettingOverlay(setting_id=setting_id, value=value, updated_at=_NOW)
            for setting_id, value in overlays.items()
        }
    )


def _openai_settings(**kwargs: Any) -> Settings:
    return Settings(**{"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-openai-test", **kwargs})


def _anthropic_settings(**kwargs: Any) -> Settings:
    return Settings(**{"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test", **kwargs})


# ============================== порядок разрешения ==========================================
def test_overlay_wins_over_env_even_when_env_says_otherwise() -> None:
    """Единственная защита от «оверлей молча игнорируется» (ADR-099 §2).

    На проде env задан почти для каждой величины; правило «env выигрывает» сделало бы функцию
    бессмысленной ровно там, где она нужна. Кейс сталкивает их ЛОБОМ: env говорит одно, оверлей —
    другое.
    """
    settings = _openai_settings(CHARACTERS_ENABLED=False)

    assert settings.characters_enabled is False  # env
    assert (
        instance_values.characters_enabled(settings=settings, snapshot=EMPTY_SNAPSHOT) is False
    )  # оверлея нет → env
    assert (
        instance_values.characters_enabled(
            settings=settings, snapshot=_snapshot(**{SETTING_CHAT_CHARACTERS_ENABLED: True})
        )
        is True
    )  # оверлей побеждает env


def test_without_overlay_the_value_is_the_env_one() -> None:
    on = _openai_settings(MODERATION_ENABLED=True)
    off = _openai_settings(MODERATION_ENABLED=False)

    assert instance_values.moderation_enabled(settings=on, snapshot=EMPTY_SNAPSHOT) is True
    assert instance_values.moderation_enabled(settings=off, snapshot=EMPTY_SNAPSHOT) is False


def test_without_env_and_without_overlay_the_value_is_the_code_default() -> None:
    settings = _openai_settings()

    assert (
        instance_values.reasoning_level(settings=settings, snapshot=EMPTY_SNAPSHOT)
        == settings.resolved_reasoning_level()
    )
    assert (
        instance_values.presets_default_locale(settings=settings, snapshot=EMPTY_SNAPSHOT)
        == settings.resolved_presets_default_locale()
    )


def test_stored_value_not_matching_the_declared_type_is_ignored() -> None:
    """Оверлей не имеет права уронить инстанс: значение не по типу ИГНОРИРУЕТСЯ (ADR-099 §9)."""
    settings = _openai_settings(MODERATION_ENABLED=True)

    assert coerce_stored_setting_value(SETTING_MODERATION_ENABLED, "нет", settings) is None
    assert coerce_stored_setting_value(SETTING_CHAT_MODELS_OFFERED, "gpt-4.1", settings) is None
    assert coerce_stored_setting_value("chat.no_such_setting", True, settings) is None
    # …а корректное значение проходит.
    assert coerce_stored_setting_value(SETTING_MODERATION_ENABLED, False, settings) is False


class _StubRow:
    def __init__(self, setting_id: str, value: Any) -> None:
        self.setting_id = setting_id
        self.value = value
        self.updated_at = _NOW


class _StubScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _StubSession:
    """Минимальная замена AsyncSession: отдаёт заранее заданные строки трёх таблиц по порядку."""

    def __init__(self, batches: list[list[Any]]) -> None:
        self._batches = batches
        self.calls = 0

    async def scalars(self, _statement: Any) -> _StubScalars:
        rows = self._batches[self.calls] if self.calls < len(self._batches) else []
        self.calls += 1
        return _StubScalars(rows)


@pytest.mark.asyncio
async def test_loading_a_bad_stored_value_emits_admin_override_value_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _openai_settings(MODERATION_ENABLED=True)
    session = _StubSession([[], [], [_StubRow(SETTING_MODERATION_ENABLED, "да")]])

    with caplog.at_level(logging.WARNING, logger="app.instance_config"):
        snapshot = await load_snapshot(session, settings)  # type: ignore[arg-type]

    assert snapshot.settings == {}
    assert "admin_override_value_ignored" in {record.message for record in caplog.records}
    # Инстанс продолжает работать на env: игнор — не отказ.
    assert instance_values.moderation_enabled(settings=settings, snapshot=snapshot) is True


# ====== §10.0: `admin_override_value_ignored.reason` — по ФАКТУ ветки, а не одним значением ==
def _ignored_reasons(records: list[logging.LogRecord]) -> list[str]:
    """`reason` из структурного события, а не из текста строки.

    ``log_event`` кладёт поля в ``extra_fields``; ассерт по тексту сообщения проверял бы
    форматирование, а не значение лейбла.
    """
    return [
        record.extra_fields["reason"]  # type: ignore[attr-defined]
        for record in records
        if record.message == "admin_override_value_ignored"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_id", "stored", "expected_reason"),
    [
        # Настройка снята с этого инстанса: несоответствие в РЕЕСТРЕ, а не в значении. Тип у
        # строки в порядке, применить её НЕКОМУ — `type_mismatch` здесь послал бы дежурного
        # искать кривое значение вместо строки, потерявшей владельца.
        ("chat.no_such_setting", True, "unknown_id"),
        # Форма верна (список строк), нарушена объявленная ГРАНИЦА `min_items: 1`.
        (SETTING_CHAT_MODELS_OFFERED, [], "out_of_range"),
        # Не тот ТИП: строка там, где объявлен bool.
        (SETTING_MODERATION_ENABLED, "да", "type_mismatch"),
    ],
    ids=["orphan_row", "constraint_violated", "wrong_type"],
)
async def test_an_ignored_row_is_labelled_by_the_fact_of_its_own_branch(
    caplog: pytest.LogCaptureFixture,
    setting_id: str,
    stored: Any,
    expected_reason: str,
) -> None:
    """ADR-099 §10.0: три достижимых значения ОДНОГО словаря, кейс на каждое.

    ⚠️ Прежде все три исхода писались `type_mismatch`, и для сироты это была ложь. Кейс падает
    при откате к общему лейблу: у сироты ожидается `unknown_id`, у нарушенной границы —
    `out_of_range`, и совпасть с `type_mismatch` они не могут.

    Во всех трёх исходах ПОВЕДЕНИЕ одно: строка игнорируется, инстанс продолжает работать на
    env — оверлей не имеет права уронить инстанс (§9). Меняется только лейбл, и кейс проверяет
    ОБА утверждения: без второго он допускал бы «правильный лейбл + уронённый инстанс».
    """
    settings = _openai_settings(MODERATION_ENABLED=True)
    session = _StubSession([[], [], [_StubRow(setting_id, stored)]])

    with caplog.at_level(logging.WARNING, logger="app.instance_config"):
        snapshot = await load_snapshot(session, settings)  # type: ignore[arg-type]

    assert _ignored_reasons(caplog.records) == [expected_reason]
    assert snapshot.settings == {}  # строка НЕ применена…
    # …и разрешение идёт по env, как будто оверлея не было вовсе.
    assert instance_values.moderation_enabled(settings=settings, snapshot=snapshot) is True
    assert set(offered_model_ids(settings=settings, snapshot=snapshot)) == set(
        settings.allowed_models_union()
    )


# ============================== состав поверхности ==========================================
def test_the_registry_declares_fourteen_rows_one_of_them_instance_conditional() -> None:
    """ADR-099 §8.1: строк ЧЕТЫРНАДЦАТЬ, и одна из них объявляется не на каждом инстансе.

    Состав зависит от инстанса по сноске ¹: `chat.anthropic_thinking_display` на OpenAI-инстансе
    потребителя не имеет, и объявить её значило бы дать оператору ручку, которая ничего не делает.
    Отсюда 14 в реестре и 13 в ответе OpenAI-инстанса — это ОДНО правило, а не расхождение.
    """
    anthropic_specs = declared_settings(_anthropic_settings())
    openai_specs = declared_settings(_openai_settings())

    assert len(anthropic_specs) == 14
    assert {spec.setting_id for spec in anthropic_specs} - {
        spec.setting_id for spec in openai_specs
    } == {SETTING_CHAT_THINKING_DISPLAY}
    assert len(openai_specs) == 13
    assert find_setting(SETTING_CHAT_THINKING_DISPLAY, _openai_settings()) is None


def test_every_declared_row_carries_non_empty_metadata_and_a_typed_value() -> None:
    settings = _anthropic_settings()

    for spec in declared_settings(settings):
        assert spec.setting_id and spec.type and spec.label and spec.group and spec.description
        value = resolve_setting(spec.setting_id, settings=settings, snapshot=EMPTY_SNAPSHOT)
        if spec.type == "bool":
            assert isinstance(value, bool)
        elif spec.type == "enum":
            assert isinstance(value, str)
        elif spec.type == "multi_enum":
            assert isinstance(value, list)
            assert all(isinstance(item, str) for item in value)
        else:
            assert isinstance(value, str)


# ------------------------------ негативный контракт поверхности -----------------------------
# ADR-099 §8.2: величина класса «секрет / инфраструктура / ресурсный лимит / денежная величина»
# в поверхность НЕ входит. Проверка ведётся ПО ЗНАЧЕНИЮ, а не по имени: в каждое запрещённое поле
# кладётся уникальный маркер, и ни одно объявленное значение не имеет права его вернуть. Список
# имён поймал бы только то, что в него вписали; маркер ловит ЛЮБУЮ новую строку, читающую
# запрещённое поле, — включая ту, чьё имя выглядит безобидно.
_FORBIDDEN_ALIASES: dict[str, str] = {
    # (а) credential
    "ANTHROPIC_API_KEY": "SENTINEL-anthropic-key",
    "OPENAI_API_KEY": "SENTINEL-openai-key",
    "FAL_API_KEY": "SENTINEL-fal-key",
    "MODERATION_API_KEY": "SENTINEL-moderation-key",
    "CLOUDPAYMENTS_API_TOKEN": "SENTINEL-cp-token",
    "JWT_PRIVATE_KEY": "SENTINEL-jwt-private",
    "KMS_LOCAL_MASTER_KEY": "SENTINEL-kms-master",
    "ADMIN_API_SECRET": "SENTINEL-admin-secret",
    "ADMIN_API_KEY": "SENTINEL-admin-key",
    "ADAPTY_WEBHOOK_SECRET": "SENTINEL-adapty-secret",
    "CLOUDPAYMENTS_WEBHOOK_TOKEN": "SENTINEL-cp-webhook",
    "PREVIEW_URL_SECRET": "SENTINEL-preview-secret",
    "METRICS_SCRAPE_TOKEN": "SENTINEL-metrics-token",
    "APPLE_TEST_SECRET": "SENTINEL-apple-secret",
    "STOREKIT_TEST_SECRET": "SENTINEL-storekit-secret",
    # (б) адрес / параметр инфраструктуры
    "DATABASE_URL": "SENTINEL-database-url",
    "REDIS_URL": "SENTINEL-redis-url",
    "SERVICE_DOMAIN": "SENTINEL-service-domain",
    "MODERATION_BASE_URL": "SENTINEL-moderation-base",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "SENTINEL-otel",
    "LOG_LEVEL": "SENTINEL-log-level",
    # (г) денежные величины
    "TOKEN_PRODUCTS": '{"SENTINEL-token-products": 1}',
    "PRODUCTS_CATALOG": '[{"productId": "SENTINEL-products-catalog"}]',
    "ADAPTY_PRODUCT_TOKENS": '{"SENTINEL-adapty-products": 1}',
    "CLOUDPAYMENTS_PRODUCT_TOKENS": '{"SENTINEL-cp-products": 1}',
    "MEDIA_MODEL_CREDITS": '{"SENTINEL-media-credits": 1}',
    # (д) параметры проверки личности и платежей
    "APPSTORE_BUNDLE_ID": "SENTINEL-bundle-id",
    "APPLE_AUDIENCE": "SENTINEL-apple-audience",
    "JWT_ISSUER": "SENTINEL-jwt-issuer",
    "JWT_AUDIENCE": "SENTINEL-jwt-audience",
    "CLOUDPAYMENTS_PAID_STATUSES": "SENTINEL-paid-statuses",
    # (е) прочие параметры обращения к провайдерам
    "MODERATION_MODEL": "SENTINEL-moderation-model",
    "TRANSCRIPTION_MODEL": "SENTINEL-transcription-model",
}

# Маркер = подстрока, уникальная для запрещённого поля. Для JSON-величин это не всё значение, а
# ключ внутри него: искать целую строку `'{"…": 1}'` в РАЗОБРАННОМ значении бессмысленно.
_SENTINEL_MARKERS: dict[str, str] = {
    alias: (
        raw
        if raw.startswith("SENTINEL")
        else next(part for part in raw.replace('"', " ").split() if part.startswith("SENTINEL"))
    )
    for alias, raw in _FORBIDDEN_ALIASES.items()
}


def test_no_declared_setting_exposes_a_secret_infra_money_or_identity_variable() -> None:
    """Свип по КЛАССАМ §8.2, проверяемый значением, а не перечнем имён.

    Кейс обязан падать при добавлении такой величины в реестр — включая случай, когда её
    объявили под безобидным `setting_id`.
    """
    settings = _anthropic_settings(**_FORBIDDEN_ALIASES)

    for spec in declared_settings(settings):
        rendered = repr(
            resolve_setting(spec.setting_id, settings=settings, snapshot=EMPTY_SNAPSHOT)
        )
        rendered += repr(spec.options(settings) if spec.options else ())
        for alias, marker in _SENTINEL_MARKERS.items():
            assert marker not in rendered, f"{spec.setting_id} раскрывает {alias}"


def test_the_secret_detector_itself_trips_on_a_rogue_row() -> None:
    """Diff-проверка САМОГО барьера: предикат обязан ловить нарушение, а не только зеленеть.

    Тест выше проходит и в том случае, если предикат сломан (маркер никогда не совпадает). Здесь
    в реестр подставляется строка, читающая `KMS_LOCAL_MASTER_KEY` под безобидным именем, и тот
    же предикат обязан её отвергнуть.
    """
    from app.instance_config.settings_registry import SettingSpec

    settings = _anthropic_settings(**_FORBIDDEN_ALIASES)
    rogue = SettingSpec(
        setting_id="chat.harmless_looking_name",
        type="string",
        label="…",
        group="Чат",
        description="…",
        env_value=lambda s: s.kms_local_master_key,
    )

    rendered = repr(rogue.env_value(settings))

    assert any(marker in rendered for marker in _SENTINEL_MARKERS.values())


def test_no_setting_id_names_a_forbidden_class_of_variable() -> None:
    """Вторая, независимая сторона того же барьера — по имени строки (ADR-099 §8.2)."""
    forbidden_tokens = (
        "api_key",
        "secret",
        "token_products",
        "private_key",
        "master_key",
        "database",
        "redis",
        "credit_cost",
        "rate_limit",
        "webhook",
        "jwt",
        "bundle",
    )

    for spec in declared_settings(_anthropic_settings()):
        lowered = spec.setting_id.lower()
        for token in forbidden_tokens:
            assert token not in lowered, f"{spec.setting_id} несёт запрещённый класс {token}"


# ============================== options выводятся из кода ===================================
def test_options_are_derived_from_the_single_declaration_in_code() -> None:
    """Кейс падает при добавлении значения в константу без появления его в `options`.

    Перечень, переписанный в тест литералом, протух бы молча: значение вне перечня показывается
    как есть, но выбрать его обратно нечем (`zh-Hans` — уже существующий пример, ADR-099 §8.1).
    """
    from app.chat.presets import SUPPORTED_PRESET_LOCALES
    from app.chat.tools import DISABLEABLE_TOOL_FAMILIES
    from app.schemas.chat import GENERATION_MODE_ORDER

    settings = _anthropic_settings()

    def options_of(setting_id: str) -> tuple[str, ...]:
        spec = find_setting(setting_id, settings)
        assert spec is not None and spec.options is not None
        return tuple(value for value, _label in spec.options(settings))

    assert options_of(SETTING_CATALOG_PRESETS_LOCALE) == tuple(SUPPORTED_PRESET_LOCALES)
    assert options_of(SETTING_CHAT_ADVERTISED_MODES) == tuple(GENERATION_MODE_ORDER)
    assert options_of(SETTING_CHAT_DISABLED_TOOL_FAMILIES) == tuple(
        sorted(DISABLEABLE_TOOL_FAMILIES)
    )
    assert options_of(SETTING_CHAT_REASONING_LEVEL) == tuple(SUPPORTED_REASONING_LEVELS)
    assert options_of(SETTING_CHAT_THINKING_DISPLAY) == tuple(SUPPORTED_ANTHROPIC_THINKING_DISPLAYS)
    assert options_of(SETTING_CHAT_DEFAULT_MODEL) == tuple(settings.allowed_models_union())
    assert options_of(SETTING_CHAT_MODELS_OFFERED) == tuple(settings.allowed_models_union())


def test_zh_hans_is_selectable_because_options_come_from_the_constant() -> None:
    """Именованная в ADR цена правила: перечень из двух локалей сделал бы инстанс невозвратимым."""
    settings = _openai_settings()
    spec = find_setting(SETTING_CATALOG_PRESETS_LOCALE, settings)
    assert spec is not None

    assert validate_setting_value(spec, "zh-Hans", settings) == "zh-Hans"


def test_model_options_are_limited_to_enabled_providers() -> None:
    """На OpenAI-инстансе без `LLM_PROVIDERS` в options нет НИ ОДНОЙ Anthropic-модели.

    Иначе оператору предложили бы модели, ключа для которых нет, и выбор любой из них дал бы
    `5xx` на каждом ходе (ADR-099 §8.1 сноска ²).
    """
    settings = _openai_settings(LLM_PROVIDERS="", ANTHROPIC_API_KEY="")
    anthropic_ids = set(_anthropic_settings().allowed_models_for("anthropic"))
    assert anthropic_ids  # предусловие: каталог Anthropic непуст

    for setting_id in (SETTING_CHAT_DEFAULT_MODEL, SETTING_CHAT_MODELS_OFFERED):
        spec = find_setting(setting_id, settings)
        assert spec is not None and spec.options is not None
        offered = {value for value, _label in spec.options(settings)}
        assert not (offered & anthropic_ids)


# ============================== коды отказа — по кейсу на каждый ============================
def test_value_outside_the_declared_options_is_rejected() -> None:
    settings = _openai_settings()
    spec = find_setting(SETTING_CHAT_REASONING_LEVEL, settings)
    assert spec is not None

    with pytest.raises(SettingValueError):
        validate_setting_value(spec, "extreme", settings)


def test_value_of_the_wrong_declared_type_is_rejected() -> None:
    settings = _openai_settings()
    spec = find_setting(SETTING_MODERATION_ENABLED, settings)
    assert spec is not None

    with pytest.raises(SettingValueError):
        validate_setting_value(spec, "true", settings)


@pytest.mark.parametrize("setting_id", [SETTING_CHAT_MODELS_OFFERED, SETTING_CHAT_ADVERTISED_MODES])
def test_empty_multi_enum_violates_the_declared_min_items(setting_id: str) -> None:
    """Пустой список НИКОГДА не означает «вернуть дефолт» — ни у одной `multi_enum`-строки.

    Ловушка, ради которой кейс записан: у `CHAT_ADVERTISED_GENERATION_MODES` пустой env штатно
    означает «применить fail-closed набор» (ADR-065 §1), и перенос этого правила на оверлей выдал
    бы за выбор оператора конфигурацию, которой он не выбирал.
    """
    settings = _openai_settings()
    spec = find_setting(setting_id, settings)
    assert spec is not None
    assert (spec.constraints or {}).get("min_items") == 1

    with pytest.raises(SettingValueError):
        validate_setting_value(spec, [], settings)


def test_block_categories_is_a_string_with_a_declared_max_length() -> None:
    settings = _openai_settings()
    spec = find_setting(SETTING_MODERATION_BLOCK_CATEGORIES, settings)
    assert spec is not None

    assert spec.type == "string"  # НЕ multi_enum: словарь категорий принадлежит провайдеру
    assert spec.options is None
    assert (spec.constraints or {}).get("max_length") == BLOCK_CATEGORIES_MAX_LENGTH
    assert validate_setting_value(spec, "x" * BLOCK_CATEGORIES_MAX_LENGTH, settings)
    with pytest.raises(SettingValueError):
        validate_setting_value(spec, "x" * (BLOCK_CATEGORIES_MAX_LENGTH + 1), settings)


@pytest.mark.parametrize("raw", ["", "  ", "hate", "violence,hate", "не-категория"])
def test_minors_stays_blocked_at_any_value_including_the_empty_string(raw: str) -> None:
    """Пол безопасности держит КОД, а не значение (ADR-099 §8.1 сноска ³).

    Пустая строка НЕ эквивалентна выключенной модерации: `sexual/minors` добавляется ПОСЛЕ
    разбора CSV. Именно это делает величину пригодной для операторской поверхности.
    """
    settings = _openai_settings()

    categories = instance_values.moderation_block_categories(
        settings=settings, snapshot=_snapshot(**{SETTING_MODERATION_BLOCK_CATEGORIES: raw})
    )

    assert "sexual/minors" in categories


def test_max_items_is_declared_by_no_row() -> None:
    """Отсутствие ключа — решение, а не пропуск: потолок набора и есть сам перечень `options`."""
    for spec in declared_settings(_anthropic_settings()):
        assert "max_items" not in (spec.constraints or {})


def test_unknown_setting_id_is_not_found_and_is_never_created() -> None:
    settings = _openai_settings()

    assert find_setting("chat.made_up", settings) is None
    with pytest.raises(KeyError):
        resolve_setting("chat.made_up", settings=settings, snapshot=EMPTY_SNAPSHOT)


# ============================== нормализация ВИДИМАЯ ========================================
def test_general_is_returned_to_the_advertised_set_and_the_order_is_canonical() -> None:
    """Код вправе нормализовать значение, но не вправе делать это молча (ADR-099 §8.1).

    Оператор вправе прислать набор без `general`; сервис принимает правку и ВОЗВРАЩАЕТ режим —
    `defaultGenerationMode` обязан присутствовать, иначе у выпущенной сборки переключатель
    остаётся без значения по умолчанию.
    """
    from app.schemas.chat import DEFAULT_GENERATION_MODE, GENERATION_MODE_ORDER

    settings = _openai_settings()

    modes = instance_values.advertised_generation_modes(
        settings=settings,
        snapshot=_snapshot(**{SETTING_CHAT_ADVERTISED_MODES: ["study_learn", "reasoning"]}),
    )

    assert DEFAULT_GENERATION_MODE in modes
    assert list(modes) == [m for m in GENERATION_MODE_ORDER if m in set(modes)]
    assert set(modes) == {DEFAULT_GENERATION_MODE, "study_learn", "reasoning"}


def test_multi_enum_validation_drops_duplicates_but_keeps_operator_order() -> None:
    settings = _openai_settings()
    spec = find_setting(SETTING_CHAT_ADVERTISED_MODES, settings)
    assert spec is not None

    assert validate_setting_value(spec, ["reasoning", "general", "reasoning"], settings) == [
        "reasoning",
        "general",
    ]


# ============================== каждая величина читает оверлей ==============================
@pytest.mark.parametrize(
    ("reader", "setting_id", "overlay_value"),
    [
        (instance_values.characters_enabled, "chat.characters_enabled", False),
        (instance_values.memory_enabled, "chat.memory_enabled", True),
        (instance_values.voice_input_enabled, "chat.voice_input_enabled", False),
        (instance_values.code_tools_enabled, "chat.code_tools_enabled", False),
        (instance_values.media_tools_enabled, "chat.media_tools_enabled", True),
        (instance_values.moderation_enabled, "moderation.enabled", True),
    ],
)
def test_every_boolean_setting_is_actually_read_through_the_overlay(
    reader: Any, setting_id: str, overlay_value: bool
) -> None:
    """«Объявлено ≠ подключено» на уровне компонента: у каждой строки есть читатель.

    Точку ПРИМЕНЕНИЯ (пользовательская ручка, чей ответ меняется) доказывают интеграционные
    кейсы — этот доказывает только устройство читателя.
    """
    settings = _anthropic_settings()

    assert (
        reader(settings=settings, snapshot=_snapshot(**{setting_id: overlay_value}))
        is overlay_value
    )
    assert (
        reader(settings=settings, snapshot=_snapshot(**{setting_id: not overlay_value}))
        is not overlay_value
    )


def test_disabled_tool_families_and_locale_and_levels_read_the_overlay() -> None:
    from app.chat.tools import DISABLEABLE_TOOL_FAMILIES

    settings = _anthropic_settings()
    family = sorted(DISABLEABLE_TOOL_FAMILIES)[0]

    assert instance_values.disabled_tool_families(
        settings=settings, snapshot=_snapshot(**{SETTING_CHAT_DISABLED_TOOL_FAMILIES: [family]})
    ) == frozenset({family})
    assert (
        instance_values.presets_default_locale(
            settings=settings, snapshot=_snapshot(**{SETTING_CATALOG_PRESETS_LOCALE: "zh-Hans"})
        )
        == "zh-Hans"
    )
    assert (
        instance_values.reasoning_level(
            settings=settings, snapshot=_snapshot(**{SETTING_CHAT_REASONING_LEVEL: "high"})
        )
        == "high"
    )
    assert (
        instance_values.anthropic_thinking_display(
            settings=settings, snapshot=_snapshot(**{SETTING_CHAT_THINKING_DISPLAY: "omitted"})
        )
        == "omitted"
    )


def test_thinking_display_on_an_openai_instance_falls_back_to_env_not_to_the_overlay() -> None:
    """Строка на этом инстансе не объявлена ⇒ её оверлей не применяется (мёртвая ручка)."""
    settings = _openai_settings(ANTHROPIC_THINKING_DISPLAY="summarized")

    assert (
        instance_values.anthropic_thinking_display(
            settings=settings, snapshot=_snapshot(**{SETTING_CHAT_THINKING_DISPLAY: "omitted"})
        )
        == settings.resolved_anthropic_thinking_display()
    )
