# ADR-081 — Per-instance denylist семейств тулов (`CHAT_DISABLED_TOOL_FAMILIES`)

- **Статус:** Accepted
- **Дата:** 2026-08-18
- **Связано:** [ADR-019](ADR-019-tools-catalog-endpoint.md), [ADR-072](ADR-072-chat-media-tools-instance-gate.md)

## Контекст

На `novirell.shop` iOS не должен предлагать `files.*`, `calendar.*`, `reminders.*`, `site.*`. Остальные инстансы эти семейства оставляют. Отдельный образ или хардкод имени инстанса в коде ломают клонирование ([ADR-017](ADR-017-shared-server-traefik-deploy.md)).

`CHAT_MEDIA_TOOLS_ENABLED` ([ADR-072](ADR-072-chat-media-tools-instance-gate.md)) закрывает только `media.*` и не подходит: нужен список семейств, а не один bool.

## Решение

Env **`CHAT_DISABLED_TOOL_FAMILIES`** (CSV, default **пусто**, per-instance).

Допустимые токены: `files`, `calendar`, `reminders`, `site`. Неизвестные — WARNING и игнор. Пусто = полный набор (поведение всех текущих инстансов).

Гейты:

1. `GET /v1/tools` — каталог без скрытых семейств (iOS не видит тулы).
2. `neutral_tool_definitions` / Anthropic / OpenAI — модель их не получает.
3. Soft `tool_not_available`, если модель всё же вызвала скрытый тул.
4. System prompt не упоминает скрытые семейства. При пустом env префикс **байт-в-байт** как раньше.

`media` этим флагом **не** выключается (остаётся [ADR-072](ADR-072-chat-media-tools-instance-gate.md)). `time.now` / `quiz.generate` не в списке.

## Операции

Только novirell:

```bash
CHAT_DISABLED_TOOL_FAMILIES=files,calendar,reminders,site
```

На остальных инстансах переменную не задавать.

## Не меняется

- Код без имени инстанса.
- Дефолт пустой → остальные инстансы идентичны текущему поведению.
- REST `/v1/media/*`, `/v1/preview` не гейтятся этим флагом.
