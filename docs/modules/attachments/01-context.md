# Attachments — Context

## Зависимости
- **API Gateway** — auth, provisioning, размещение `/v1/attachments`. Multipart-загрузка имеет собственный transport-лимит (см. [05-security.md](05-security.md)), отличный от JSON `≤512KB`.
- **attachments** таблица (BYTEA на старте).

## Потребители
- **chat-orchestrator** — резолвит `attachments[]` из `/chat/run` в Anthropic content-блоки ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)); проставляет `attachments.session_id` при первом использовании.
- ~~**workspaces** — `workspace_files.attachment_id` ссылается на `attachments`~~ **(больше не актуально, [ADR-036 §4](../../adr/ADR-036-workspaces-implementation.md)):** workspace-файлы-знания хранятся в собственном BYTEA-столбце `workspace_files.content`, **не** ссылаются на `attachments`. Этот модуль больше не предпосылка для workspaces.

## Соседи
- **website-builder** — разделяет подход «контент в БД» и общий [TD-009](../../100-known-tech-debt.md) (миграция в object-storage), но это **разные** таблицы (`site_files` ≠ `attachments`).

## Границы
- Attachments не вызывает Anthropic сам; только хранит байты/extracted_text и отдаёт их orchestrator при сборке запроса.
- Извлечение текста из PDF — синхронно при загрузке (библиотека из [02-tech-stack.md](../../02-tech-stack.md)).
