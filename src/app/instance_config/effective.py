"""«Действующие настройки» инстанса: ``Settings`` с наложенным оверлеем ADR-116 (§5).

**Единая точка резолва.** Величины ADR-116 — креденшлы (§2.1) и строки провайдера,
StoreKit, CloudPayments и карт (§4.1) — читаются потребителями НЕ из ``get_settings()``, а из
копии ``Settings``, в которой поля этих величин заменены значениями оверлея. Тогда методы
``Settings`` (``credits_providers()``, ``apple_audience_resolved()``,
``cloudpayments_checkout_configured()``, цепочки ключей ротации) работают на действующих
значениях без второй копии их логики. Прямое чтение ``get_settings().<поле>`` для величины
ADR-116 — дефект класса «объявлено ≠ подключено».

Порядок разрешения — **оверлей → env → дефолт кода**. Пустой оверлей возвращает ТОТ ЖЕ объект
``get_settings()``: инстанс без строк оверлея работает бит-в-бит как до выката.

⚠️ **Производное поведение ADR-116 включается ТОЛЬКО строкой оверлея.** Правила, которые ADR
выводит из режима (например, §4.2: «в песочнице bundle не сверяется, в production тестовая
ветка выключена»), НЕ вычисляются из env-значений: на env-инстансе без строки ``storekit.mode``
действуют прежние флаги как есть. Потребитель узнаёт, пришла ли величина из оверлея, через
``overlaid_setting_ids``, а не сравнением значений.

Копия пересобирается при смене снимка (кэш по паре «базовые настройки × снимок»).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from app.config import Settings, get_settings
from app.instance_config.snapshot import InstanceConfigSnapshot, get_snapshot

_PROVIDERS = ("openai", "anthropic")

# Кэш: ключ — (id базовых настроек, id снимка); значение держит оба объекта СИЛЬНО, поэтому
# их идентификаторы не переиспользуются, пока запись жива. Размер ограничен.
_CACHE_MAX = 32
_cache: OrderedDict[tuple[int, int], tuple[Settings, InstanceConfigSnapshot, Settings]] = (
    OrderedDict()
)
# Выпущенные копии → их базовые настройки (для корня и для идемпотентности наложения).
_produced: OrderedDict[int, tuple[Settings, Settings, frozenset[str]]] = OrderedDict()


def other_provider(provider: str) -> str:
    """Второй провайдер пары ``openai``/``anthropic``."""
    return "anthropic" if provider == "openai" else "openai"


def normalized_provider(value: str) -> str:
    """Каноническое имя провайдера — то же правило, что ``Settings._normalized_llm_provider``."""
    return "openai" if value.strip().lower() == "openai" else "anthropic"


def providers_named(raw: str) -> tuple[str, ...]:
    """Провайдеры, названные в CSV ``LLM_PROVIDERS`` (только известные)."""
    named: list[str] = []
    for part in raw.split(","):
        provider = part.strip().lower()
        if provider in _PROVIDERS and provider not in named:
            named.append(provider)
    return tuple(named)


def overlay_updates(base: Settings, snapshot: InstanceConfigSnapshot) -> dict[str, Any]:
    """Поля ``Settings``, заменяемые оверлеем ``snapshot``. Пусто — оверлея ADR-116 нет."""
    from app.instance_config.credentials import find_credential
    from app.instance_config.settings_registry import (
        SETTING_CHAT_MAPS_TOOLS_ENABLED,
        SETTING_CLOUDPAYMENTS_APP_ID,
        SETTING_CLOUDPAYMENTS_PAY_PAGE_PROXY,
        SETTING_LLM_DUAL_ENABLED,
        SETTING_LLM_PROVIDER,
        SETTING_STOREKIT_BUNDLE_ID,
        SETTING_STOREKIT_MODE,
        STOREKIT_MODE_PRODUCTION,
    )

    updates: dict[str, Any] = {}
    for credential_id, credential in snapshot.credentials.items():
        spec = find_credential(credential_id)
        if spec is not None:
            updates[spec.settings_field] = credential.value

    settings = snapshot.settings
    provider_overlay = settings.get(SETTING_LLM_PROVIDER)
    if provider_overlay is not None:
        updates["llm_provider"] = str(provider_overlay.value)
    dual_overlay = settings.get(SETTING_LLM_DUAL_ENABLED)
    if dual_overlay is not None:
        provider = normalized_provider(str(updates.get("llm_provider", base.llm_provider)))
        # `true` ⇔ второй провайдер — тот из пары, что не выбран основным (ADR-073): им и
        # заполняется CSV; ключ у него проверяет сам `credits_providers()`.
        updates["llm_providers_raw"] = other_provider(provider) if dual_overlay.value else ""
    mode_overlay = settings.get(SETTING_STOREKIT_MODE)
    if mode_overlay is not None:
        production = mode_overlay.value == STOREKIT_MODE_PRODUCTION
        # Три производные режима (ADR-116 §4.2): песочница — без привязки цепочки к корню
        # Apple и с тестовой веткой HS256; production — привязка цепочки, тестовая ветка
        # выключена.
        updates["appstore_environment"] = "production" if production else "sandbox"
        updates["storekit_dev_skip_cert_chain_verification"] = not production
        updates["storekit_test_mode"] = not production
    simple = (
        (SETTING_STOREKIT_BUNDLE_ID, "appstore_bundle_id"),
        (SETTING_CLOUDPAYMENTS_APP_ID, "cloudpayments_app_id"),
        (SETTING_CLOUDPAYMENTS_PAY_PAGE_PROXY, "cloudpayments_pay_page_proxy_enabled"),
        (SETTING_CHAT_MAPS_TOOLS_ENABLED, "maps_tools_enabled"),
    )
    for setting_id, field_name in simple:
        overlay = settings.get(setting_id)
        if overlay is not None:
            updates[field_name] = overlay.value
    return updates


def base_settings_of(settings: Settings) -> Settings:
    """Базовые (env) настройки, из которых выпущена копия; для базовых — они сами."""
    entry = _produced.get(id(settings))
    if entry is not None and entry[0] is settings:
        return entry[1]
    return settings


def overlaid_setting_ids(settings: Settings) -> frozenset[str]:
    """Строки ``admin_settings``, наложенные на эти настройки. Базовые (env) — пустое множество.

    Нужна там, где ADR предписывает поведение, производное от строки оверлея (§4.2): оно обязано
    включаться наличием строки, а не значением env-поля, иначе меняется env-инстанс без правок.
    """
    entry = _produced.get(id(settings))
    if entry is not None and entry[0] is settings:
        return entry[2]
    return frozenset()


def is_effective(settings: Settings) -> bool:
    """Выпущена ли эта копия наложением оверлея."""
    entry = _produced.get(id(settings))
    return entry is not None and entry[0] is settings


def apply_overlay(base: Settings, snapshot: InstanceConfigSnapshot) -> Settings:
    """Наложить оверлей ``snapshot`` на ``base``. Без оверлея — ``base`` без копирования.

    ``base``, выпущенный этой функцией раньше, заменяется своим корнем: наложение считается
    от env, а не от прежней копии, иначе удалённая строка оверлея переживала бы удаление.
    """
    root = base_settings_of(base)
    key = (id(root), id(snapshot))
    cached = _cache.get(key)
    if cached is not None and cached[0] is root and cached[1] is snapshot:
        _cache.move_to_end(key)
        return cached[2]
    updates = overlay_updates(root, snapshot)
    result = root.model_copy(update=updates) if updates else root
    _cache[key] = (root, snapshot, result)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    if result is not root:
        _produced[id(result)] = (result, root, frozenset(snapshot.settings))
        while len(_produced) > _CACHE_MAX:
            _produced.popitem(last=False)
    return result


def get_effective_settings() -> Settings:
    """Действующие настройки процесса: ``get_settings()`` + текущий снимок оверлея."""
    return apply_overlay(get_settings(), get_snapshot())


def effective_settings(settings: Settings | None = None) -> Settings:
    """Действующие настройки для переданных (или процессных) базовых настроек.

    Уже выпущенная копия возвращается как есть: вызывающий, получивший действующие настройки
    в начале запроса, видит их согласованными до конца запроса.
    """
    if settings is None:
        return get_effective_settings()
    if is_effective(settings):
        return settings
    return apply_overlay(settings, get_snapshot())
