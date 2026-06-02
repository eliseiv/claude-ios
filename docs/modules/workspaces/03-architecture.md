# Workspaces — Architecture

## Размещение
Пакет `src/app/workspaces/`: репозитории над `workspace_projects`/`workspace_files` + use-cases (CRUD, add/remove/list files) + роутер `/v1/workspaces/*`.

## Подача контекста Claude (вызывается orchestrator)
- При `/chat/run` с `workspaceProjectId`: orchestrator запрашивает у workspaces `(instructions, files)` для проекта владельца.
- `instructions` → добавляется к base-system-prompt **после** assistant_mode prompt ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)). Порядок: base(assistant_mode) → workspace.instructions → пользовательский message.
- `workspace_files` → для document `extracted_text` вставляется как текстовый контекст; image — как vision-блок (механика общая с [ADR-014](../../adr/ADR-014-multimodal-attachments.md), через `attachments`).
- Суммарный размер инжектируемого контекста ограничен `WORKSPACE_CONTEXT_MAX_CHARS`; превышение → усечение (порядок: новые/важные файлы первыми; точная стратегия при росте — [Q-013-1](../../99-open-questions.md)).

## Привязка/изоляция
- `workspace_project_id` фиксируется на сессию при создании (orchestrator), не меняется задним числом.
- Все запросы скоупятся `WHERE user_id = :sub` (workspace) и проверкой принадлежности `attachment` тому же пользователю при привязке файла.

## Инварианты
- Workspace ≠ website-builder project ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)); `workspace_project_id` (UUID FK) ≠ `project_id` (TEXT).
- Файлы-контекст не дублируют BYTEA — хранятся в `attachments`; `workspace_files` — только связь.
- Удаление workspace не удаляет чаты (SET NULL) и не удаляет сами `attachments` (принадлежат пользователю).
