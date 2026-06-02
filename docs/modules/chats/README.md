# Module: Chats (история чатов / CRUD)

- Статус: Реализован (Спринт 1)
- Ответственность: список чатов пользователя (заголовок/preview/updatedAt, пагинация, сортировка pinned→updated), поиск, переименование (rename), удаление, закрепление (pin), просмотр истории шагов и steps-view. Работает поверх существующих `chat_sessions`/`chat_steps`/`tool_calls`.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — общий [03-data-model.md](../../03-data-model.md). В Спринте 1 (миграция `0004`) `chat_sessions` расширен `title`/`is_pinned`/`assistant_mode` + индекс `ix_sessions_user_pinned_updated`. Колонка `workspace_project_id` и индекс `ix_sessions_workspace` — **СПРИНТ 2 (отложено)**, вместе с модулем `workspaces` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)); в Спринте 1 их нет. Отдельного `04-data-model.md` нет.

## DoD
- `GET /v1/chats` (список с `title`/`preview`/`updatedAt`/`isPinned`, пагинация, поиск `q`), `GET /v1/chats/{id}` (история шагов), `GET /v1/chats/{id}/steps` (steps-view), `PATCH /v1/chats/{id}` (rename/pin), `DELETE /v1/chats/{id}`.
- Автогенерация `title` из первого user-сообщения; rename перезаписывает.
- Сортировка: `is_pinned DESC, updated_at DESC`. Изоляция владельца (`user_id == sub`).
- Не ломает `/chat/run`/`/chat/tool-result` (читает те же таблицы; запись `title` при создании сессии).

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap расширение). Расширение `chat_sessions`. См. [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md), [figma-gap-analysis.md](../../figma-gap-analysis.md).
- 2026-06-02 (Спринт 1, backend): реализованы `GET /v1/chats` (список/поиск/курсор), `GET /v1/chats/{id}` (история), `GET /v1/chats/{id}/steps` (steps-view), `PATCH /v1/chats/{id}` (rename/pin), `DELETE /v1/chats/{id}`. Автоген `title` в orchestrator при создании сессии. Поле `workspaceProjectId` в ответе списка пока `null` (колонка `chat_sessions.workspace_project_id` и фильтр по ней — Спринт 2, вместе с таблицей `workspace_projects`). Миграция `0004` (поля `title`/`assistant_mode`/`is_pinned` + индекс `ix_sessions_user_pinned_updated`).
- 2026-06-02 (architect, docs↔код sync): приведены chats-docs в соответствие с фактически поставленным кодом — `workspace_project_id` / query-фильтр `workspaceProjectId` / индекс `ix_sessions_workspace` помечены как **СПРИНТ 2 (отложено)** и убраны из scope Спринта 1 / миграции `0004` (в коде они отсутствуют, см. миграцию `0004`, `chats/service.py`, router). Зарегистрирован [TD-012](../../100-known-tech-debt.md) (N+1 на `preview` в `list_chats`).
