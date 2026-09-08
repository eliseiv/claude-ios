# Module: Preferences

- Статус: Реализован (Спринт 1)
- Ответственность: пользовательские настройки — `defaultAssistantMode` (chat|code), `notificationsEnabled`, `defaultVoiceId` (голос озвучки по умолчанию, [ADR-100](../../adr/ADR-100-assistant-speech-output.md)), дефолты Code-context (`codeDefaults`). Источник дефолта `assistantMode` для `/chat/run` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `user_preferences` (таблица 12, миграция `0004`, общий [03-data-model.md](../../03-data-model.md)).

## DoD
- `GET /v1/preferences` / `PATCH /v1/preferences`. Дефолты при отсутствии строки.
- `defaultAssistantMode` используется orchestrator как fallback для `assistantMode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)).
- `notificationsEnabled` — единый источник настройки уведомлений (push-токены — модуль notifications).
- `defaultVoiceId` — голос озвучки по умолчанию; читается **на каждом синтезе** ([ADR-100](../../adr/ADR-100-assistant-speech-output.md)), а не при создании сессии, поэтому смена настройки действует на уже начатые чаты.

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). Таблица `user_preferences`. [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md). См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
- 2026-06-02 (Спринт 1, backend): реализованы `GET /v1/preferences` (дефолты `chat`/`true`/`{}` при отсутствии строки) и `PATCH /v1/preferences` (частичное обновление + upsert; `defaultAssistantMode` chat|code, `notificationsEnabled`, `codeDefaults` ≤ 8 KB, без секретов). orchestrator использует `defaultAssistantMode` как fallback для `assistantMode`. Миграция `0004` (таблица `user_preferences`). Тесты зелёные (offline-сьют 681/681).
- 2026-06-16 (architect): смена контрактного дефолта `notificationsEnabled` `true` → `false` ([ADR-032](../../adr/ADR-032-notifications-enabled-default-false.md)) — privacy-by-default, iOS запрашивает системное разрешение на push сначала. Меняется только дефолт для новых/без-строки пользователей (сервисный `_defaults()` + `server_default` колонки); существующие строки `user_preferences` НЕ трогаются (миграция без backfill). Указания backend: см. ADR-032.
- 2026-09-08 (architect): добавлено поле `defaultVoiceId` ([ADR-100](../../adr/ADR-100-assistant-speech-output.md)). Nullable, миграция `0032_user_default_voice` (номер по факту приземления: волна экономики CRM заняла `0033`) (expand-only, без backfill, без индекса, без FK — реестр голосов живёт в коде, как `model`/`character_id`). `null` = голос инстанса (`TTS_DEFAULT_VOICE_ID`). Валидация на `PATCH` по `selectable`-записям реестра: вне набора → `422 unknown_voice`; при `VOICE_OUTPUT_ENABLED=false` непустое значение → `422 voice_output_disabled`, но **уже сохранённое значение продолжает отдаваться** в `GET` (симметрия с `characterId` при снятом флаге персонажей). **Контраст с соседним `defaultAssistantMode` помечен:** тот читается один раз (режим фиксируется на сессию), `defaultVoiceId` — на каждом синтезе (голос на сессии не фиксируется).
