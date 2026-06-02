# Workspaces — Testing

## Unit
- Лимиты длины (`name`/`description`/`instructions`).
- Усечение суммарного контекста до `WORKSPACE_CONTEXT_MAX_CHARS`.
- Порядок сборки prompt: base(assistant_mode) → instructions → message.

## Integration
- CRUD workspace; чужой → `404`.
- Привязка файла: свой `attachmentId` → `201`; чужой → `403`/`404`; дубликат → `409`.
- DELETE workspace: `workspace_files` cascade; `chat_sessions.workspace_project_id` → NULL (чат жив); `attachments` не удалены.
- `/chat/run` с `workspaceProjectId`: instructions попадают в system-prompt, файлы — в контекст; чужой workspace → `404`.
- Разведение: `workspace_project_id` не путается с website-builder `project_id` (разные сессии/таблицы).

## Изоляция
- Все эндпоинты скоупятся `sub`.
