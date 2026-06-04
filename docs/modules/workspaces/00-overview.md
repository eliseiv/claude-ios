# Workspaces — Overview

## Назначение
Рабочее пространство чатов (дизайн «Project»): контейнер с именем, описанием, кастомными инструкциями (system-prompt) и файлами-контекстом (PDF и т.п.), внутри которого ведутся чаты с общим контекстом.

## Разведение с website-builder ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md))
- **workspace** (этот модуль) — **вход/контейнер**: контекст + инструкции для Claude, группировка чатов.
- **website-builder `projects`** — **вывод/артефакт**: сгенерированные Claude статические сайты (`site_files`, server-side `site.*`). Другая сущность, не переиспользуется.
- `chat_sessions.workspace_project_id` (UUID FK) ≠ `chat_sessions.project_id` (TEXT, website-builder external id). Разные поля.

## Scope
- CRUD workspace: `POST/GET/PATCH/DELETE /v1/workspaces[/{id}]`.
- Управление `instructions` — через PATCH workspace (`instructions` — поле).
- Файлы-контекст: `POST /v1/workspaces/{id}/files` (привязать ранее загруженный `attachmentId`), `GET` (список), `DELETE /v1/workspaces/{id}/files/{fileId}`.
- Привязка чатов: при `/chat/run` с `workspaceProjectId`; список чатов workspace — `GET /v1/chats?workspaceProjectId=...` (модуль chats).
- Подача `instructions` + `workspace_files` Claude при генерации в сессии workspace.

## Out of scope
- Совместный доступ/шеринг workspace между пользователями.
- Версионирование инструкций/файлов.
- RAG/векторный поиск по файлам (на старте — вставка `extracted_text` с лимитом, [Q-013-1](../../99-open-questions.md)).

## Бизнес-правила
- BR-WS-1: workspace принадлежит пользователю (`user_id == sub`); чужой → `404`.
- BR-WS-2: `instructions` (nullable) добавляются к base-system-prompt (после assistant_mode prompt, [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)) при генерации в сессии этого workspace.
- BR-WS-3: файл-контекст — это `attachment` ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)), привязанный к workspace через `workspace_files`. Привязываемый `attachmentId` обязан принадлежать тому же пользователю. ⚠️ **Зависимость:** workspace file-context требует таблицы `attachments`, которая на MVP **отложена** ([TD-015](../../100-known-tech-debt.md); chat-вложения на MVP — inline base64 без таблицы, [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)). Workspaces (Спринт 2) реализует двухшаговый attachments-upload как свою предпосылку, либо подаёт файлы-контекст альтернативным способом — решение при реализации модуля.
- BR-WS-4: подача `workspace_files` Claude — `extracted_text` (document) как текстовый контекст / image как vision; суммарный размер контекста ограничен ([Q-013-1](../../99-open-questions.md)).
- BR-WS-5: удаление workspace → `workspace_files` cascade; `chat_sessions.workspace_project_id` → NULL (чаты сохраняются).
