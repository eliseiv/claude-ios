# Workspaces — Implementation Phases

Спринт 2. Зависит от миграции `0004` (`workspace_projects`/`workspace_files`/`chat_sessions.workspace_project_id`) и от таблицы `attachments` (Спринт 3-модуль, но таблица создаётся в `0004`).

1. **Phase 1 — миграция:** таблицы `workspace_projects`/`workspace_files`, колонка `chat_sessions.workspace_project_id` + индексы.
2. **Phase 2 — CRUD workspace:** `POST/GET/PATCH/DELETE /v1/workspaces[/{id}]`.
3. **Phase 3 — файлы-контекст:** `POST/GET/DELETE /v1/workspaces/{id}/files[/{fileId}]` (привязка `attachmentId` владельца).
4. **Phase 4 — интеграция с orchestrator:** подача `instructions` + `workspace_files` в prompt/контекст при `/chat/run` с `workspaceProjectId`.

> Файлы-контекст требуют рабочего `POST /v1/attachments` (модуль attachments). Если attachments-endpoint ещё не готов, Phase 1–2 (CRUD без файлов) реализуемы независимо; Phase 3–4 ждут attachments.
