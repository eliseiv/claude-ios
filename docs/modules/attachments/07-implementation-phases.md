# Attachments — Implementation Phases

> ⚠️ **Модуль отложен ([TD-015](../../100-known-tech-debt.md)).** На MVP мультимодальный ввод реализован inline base64 в `/v1/chat/run` ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), реализует chat-orchestrator) — без этого модуля и без таблицы `attachments`. Фазы ниже относятся к будущей двухшаговой модели ([ADR-014](../../adr/ADR-014-multimodal-attachments.md) transport Superseded); миграция `0004` таблицу `attachments` **НЕ создаёт**.

Будущий путь (после закрытия [TD-015](../../100-known-tech-debt.md)). Зависит от **отдельной будущей миграции** (таблица `attachments`, enum `attachment_kind` — НЕ `0004`) и от PDF-парсера в стеке. (Enum `attachment_kind` уже объявлен в сводном DDL [03-data-model.md](../../03-data-model.md), но миграцией на MVP не применяется.)

1. **Phase 1 — миграция + стек:** таблица `attachments` (отдельная будущая миграция, НЕ `0004`), enum `attachment_kind`; добавить PDF-extractor в [02-tech-stack.md](../../02-tech-stack.md).
2. **Phase 2 — upload:** `POST /v1/attachments` (multipart, magic-bytes detection, allowlist, size-guard, extract_text), `GET`/`DELETE`.
3. **Phase 3 — резолв:** утилита `resolve_attachments` + интеграция в `/chat/run` (`attachments[]` → Anthropic content-блоки, проставление `session_id`).
4. **Phase 4 (опц.) — orphan cleanup:** при наличии планировщика (иначе [TD-010](../../100-known-tech-debt.md)).

> Workspace-файлы (`workspace_files`, модуль workspaces, Спринт 2) зависят от таблицы `attachments` (FK). Обе таблицы **отложены** ([TD-015](../../100-known-tech-debt.md)) и на MVP миграцией `0004` **не создаются** — `0004` создаёт только `user_preferences` (+ поля `chat_sessions`/`users`). При реализации двухшаговой модели таблица `attachments` создаётся отдельной будущей миграцией и должна предшествовать `workspace_files`.
