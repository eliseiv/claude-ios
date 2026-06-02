# Workspaces — Context

## Зависимости
- **API Gateway** — auth, provisioning, роуты `/v1/workspaces/*`.
- **attachments** ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)) — файлы-контекст хранятся в `attachments`; `workspace_files` ссылается на `attachments.id`. Workspace-файл сперва загружается через `POST /v1/attachments`, затем привязывается.
- **workspace_projects**/**workspace_files** таблицы.

## Потребители
- **chat-orchestrator** — при `/chat/run` с `workspaceProjectId` подмешивает `instructions` в system-prompt и `workspace_files` в контекст ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).
- **chats** — фильтр списка чатов по `workspace_project_id`.

## Соседи (важно не путать)
- **website-builder** — отдельная сущность `projects`/`site_files` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)). Workspace ≠ website-builder project.

## Границы
- Workspaces не вызывает Anthropic; предоставляет orchestrator данные контекста (instructions + файлы) по запросу.
- Не дублирует BYTEA-хранение — использует `attachments`.
