# Workspaces — Implementation Phases

Спринт 2. Предпосылка `workspace_projects` + `chat_sessions.workspace_project_id` создаётся **отдельной будущей миграцией** (НЕ `0004` — `0004` создаёт только `user_preferences` + поля `chat_sessions`/`users`). Таблицы `workspace_files` и `attachments` **отложены** ([TD-015](../../100-known-tech-debt.md)) — на MVP миграцией не создаются; Phase 3–4 (файлы-контекст) ждут реализации двухшаговой модели.

1. **Phase 1 — миграция:** таблица `workspace_projects` + колонка `chat_sessions.workspace_project_id` + индексы (отдельная будущая миграция, НЕ `0004`). Таблица `workspace_files` — только при снятии отложенности (зависит от `attachments`, [TD-015](../../100-known-tech-debt.md)).
2. **Phase 2 — CRUD workspace:** `POST/GET/PATCH/DELETE /v1/workspaces[/{id}]`.
3. **Phase 3 — файлы-контекст:** `POST/GET/DELETE /v1/workspaces/{id}/files[/{fileId}]` (привязка `attachmentId` владельца).
4. **Phase 4 — интеграция с orchestrator:** подача `instructions` + `workspace_files` в prompt/контекст при `/chat/run` с `workspaceProjectId`.

> Файлы-контекст требуют рабочего `POST /v1/attachments` (модуль attachments) — **отложен на MVP** ([TD-015](../../100-known-tech-debt.md), таблица `attachments` миграцией не создаётся). Phase 1–2 (CRUD workspace без файлов) реализуемы независимо; Phase 3–4 ждут реализации двухшаговой модели вложений.
