# Module: Workspaces (рабочие пространства чатов)

- Статус: Спроектирован (backend — Спринт 2)
- Ответственность: проекты-воркспейсы = name + description + кастомные `instructions` (system-prompt) + прикреплённые файлы-контекст + список чатов проекта. **НЕ путать** с website-builder `projects` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `workspace_projects` (таблица 13) + `chat_sessions.workspace_project_id`: предпосылка Спринта 2, создаётся **отдельной будущей миграцией** (НЕ `0004` — `0004` создаёт только `user_preferences` + поля `chat_sessions`/`users`). `workspace_files` (таблица 14) и `attachments` — **отложены** ([TD-015](../../100-known-tech-debt.md)), на MVP миграцией не создаются; файлы-контекст (хранение в `attachments`, [ADR-014](../../adr/ADR-014-multimodal-attachments.md)) появляются только при реализации двухшаговой модели.

## DoD
- CRUD `/v1/workspaces` (name/description/instructions).
- Add/remove файлов-контекста (через `attachments`), list файлов.
- Привязка чатов к workspace (`chat_sessions.workspace_project_id`); список чатов workspace (через модуль chats).
- `instructions` + `workspace_files` подаются Claude при генерации в сессии workspace ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md). Таблицы `workspace_projects`/`workspace_files`. См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
