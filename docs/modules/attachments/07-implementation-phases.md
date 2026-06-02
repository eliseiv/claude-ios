# Attachments — Implementation Phases

Спринт 3 (мультимодальность). Зависит от миграции `0004` (таблица `attachments`, enum `attachment_kind`) и от PDF-парсера в стеке.

1. **Phase 1 — миграция + стек:** таблица `attachments`, enum `attachment_kind`; добавить PDF-extractor в [02-tech-stack.md](../../02-tech-stack.md).
2. **Phase 2 — upload:** `POST /v1/attachments` (multipart, magic-bytes detection, allowlist, size-guard, extract_text), `GET`/`DELETE`.
3. **Phase 3 — резолв:** утилита `resolve_attachments` + интеграция в `/chat/run` (`attachments[]` → Anthropic content-блоки, проставление `session_id`).
4. **Phase 4 (опц.) — orphan cleanup:** при наличии планировщика (иначе [TD-010](../../100-known-tech-debt.md)).

> Workspace-файлы (модуль workspaces, Спринт 2) зависят от таблицы `attachments` — миграция `0004` должна предшествовать. Если workspaces реализуется раньше attachments-endpoint'ов, таблица `attachments` всё равно создаётся в `0004` (общая).
