# ADR-119 — Каталоги: локали `de` / `fr` / `it` в `SUPPORTED_PRESET_LOCALES`

- **Статус:** Accepted. **Состояние реализации:** код не написан этим проходом (зона `backend`).
- **Дата:** 2026-09-28
- **Связано:** [ADR-049](ADR-049-presets-localization.md) (набор и резолв), [ADR-097](ADR-097-character-personas.md) (characters), [ADR-100](ADR-100-assistant-speech-output.md) (voices), [ADR-083](ADR-083-preset-subcategories-and-descriptions.md) (description), [ADR-099](ADR-099-crm-admin-economics-and-instance-settings.md) (`catalog.presets_default_locale` → константа). [TD-035](../100-known-tech-debt.md).
- **Характер:** расширение allowlist локалей каталогов. Миграций нет. Контракт аддитивен: бывшие `en`/`ru`/`zh-Hans` без изменений; новые теги перестают давать `422` на `?locale=`.

## Контекст

`SUPPORTED_PRESET_LOCALES` в `src/app/chat/presets.py` = `("en", "ru", "zh-Hans")`. Та же константа и `resolve_presets_locale` обслуживают `GET /v1/presets`, `GET /v1/characters`, `GET /v1/voices`. Приложение запрашивает `?locale=de|fr|it` → **`422`** (`locale '<x>' is not supported`).

**Решение владельца (принято):** поддержать `de`, `fr`, `it` (как в приложении). Имена (и прочие локализуемые поля): допустим **EN-fallback per-field**, если переводов ещё нет.

## Решение

### §1. Набор

`SUPPORTED_PRESET_LOCALES` расширяется до:

```text
("en", "ru", "zh-Hans", "de", "fr", "it")
```

Порядок в кортеже — стабильный для итераций/опций админки; семантика — множество. Канон и fallback по-прежнему **`en`** ([ADR-049 §1](ADR-049-presets-localization.md)).

Канонизация: primary-subtag (`de-DE`→`de`, `fr-FR`→`fr`, `it-IT`→`it`); для `zh-*` — прежние правила → `zh-Hans`. Явный `?locale=` вне набора → **`422`** (без изменений политики).

### §2. Per-field EN-fallback

Для **каждого** локализуемого поля каталогов (`title`/`prompt`/`description` пресетов; `name`/`tagline` персонажей; `name` голосов):

- если для выбранной локали строка отсутствует или пуста → берётся `en` того же поля;
- заполнять `de`/`fr`/`it` словари **не обязательно** для приёмки этого ADR: достаточно членства в наборе + работающий fallback (как уже для незаполненного `zh-Hans` у персонажей, [ADR-097](ADR-097-character-personas.md)).

Заполнение переводов — последующая правка реестров без нового ADR, пока набор и fallback не меняются.

### §3. Поверхности

Один набор на все три каталога и на `options` настройки `catalog.presets_default_locale` ([ADR-099](ADR-099-crm-admin-economics-and-instance-settings.md)): значение `options` **не** дублировать списком в docs — источник после реализации = константа в коде. Документы, где набор был перечислен (`en`/`ru`/`zh-Hans`), приводятся к актуальной **норме** или к отсылке «`SUPPORTED_PRESET_LOCALES`» (без утверждения, что код уже расширен).

`PRESETS_DEFAULT_LOCALE` / graceful fallback вне набора → `en` + WARNING — без изменений. Исходящий `context.locale` пейволла ([ADR-098](ADR-098-broadapps-paywall-experiments-and-default-product.md)) по-прежнему **без** клампа к набору.

### §4. Что не меняется

Резолв-цепочка ADR-049 §3; `422` на явный неподдерживаемый `locale`; независимость `ChatRunRequest.context.locale` ([ADR-037](ADR-037-chatrunrequest-context-allowlist-injection.md)).

## Последствия

- `GET /v1/voices?locale=de` (и fr/it) → `200` с `locale` из набора и именами через EN-fallback при отсутствии перевода.
- Backend: константа + при необходимости ключи в словарях; qa — `?locale=de|fr|it` → 200, `?locale=xx` → 422; регресс en/ru/zh-Hans.

## Альтернативы

- **Тихий fallback `de`→`en` на query без `422`** — отклонено: ломает симметрию ADR-049 / ADR-034 (явный запрос вне набора строг).
- **Отдельный allowlist только для voices** — отклонено: один резолвер, [TD-035](../100-known-tech-debt.md).
