# Workspaces — API Contracts

JWT (`bearerAuth`), владелец = `sub`. Чужой/несуществующий workspace → `404`. Все схемы — `extra='forbid'`.
Реализация — [ADR-036](../../adr/ADR-036-workspaces-implementation.md).

Лимиты длины: `name` ≤ 120, `description` ≤ 1000, `instructions` ≤ 16000 символов.

## POST /v1/workspaces
Создать рабочее пространство.
### Request
```json
{ "name": "string", "description": "string (optional)", "instructions": "string (optional)" }
```
- `name` обязателен, непустой после strip.

### Response (201)
```json
{
  "id": "uuid",
  "name": "string",
  "description": "string | null",
  "instructions": "string | null",
  "createdAt": "ISO8601",
  "updatedAt": "ISO8601"
}
```

## GET /v1/workspaces
Список workspace пользователя. **Курсорная пагинация** (как `GET /v1/chats`): query `cursor` (opaque, opt.), `limit` (1..100, дефолт 50). Порядок — `updatedAt DESC`.
### Response (200)
```json
{
  "items": [
    { "id": "uuid", "name": "string", "description": "string | null", "updatedAt": "ISO8601", "fileCount": 0, "chatCount": 0 }
  ],
  "nextCursor": "string | null"
}
```
- `fileCount` — число файлов-знаний (count `workspace_files`); `chatCount` — число чатов проекта (count `chat_sessions WHERE workspace_project_id = id`).

## GET /v1/workspaces/{workspace_id}
Полный объект workspace (включая `instructions` и список файлов).
### Response (200)
```json
{
  "id": "uuid",
  "name": "string",
  "description": "string | null",
  "instructions": "string | null",
  "files": [
    { "fileId": "uuid", "filename": "string", "mediaType": "string", "size": 0, "hasExtractedText": true, "createdAt": "ISO8601" }
  ],
  "createdAt": "ISO8601",
  "updatedAt": "ISO8601"
}
```
- `files[]` — метаданные файлов-знаний (без `content`/`extractedText` — тело не отдаётся в API; контекст подаётся только модели). `hasExtractedText` — извлечён ли текст (для UI).

## PATCH /v1/workspaces/{workspace_id}
Обновление `name`/`description`/`instructions` (любое непустое подмножество; хотя бы одно поле).
### Request
```json
{ "name": "string", "description": "string", "instructions": "string" }
```
- Те же лимиты длины. `description`/`instructions` можно очистить, передав `null`.
### Response (200)
Полный объект (как `GET /{workspace_id}`).

## DELETE /v1/workspaces/{workspace_id}
### Response (200)
```json
{ "deleted": true }
```
- CASCADE удаляет `workspace_files`; `chat_sessions.workspace_project_id` → NULL (чаты остаются как «чистые», [ADR-036 §5](../../adr/ADR-036-workspaces-implementation.md)).

---

## Файлы-знания workspace (под-фаза 3B)

> Файлы-знания хранятся в **собственной таблице `workspace_files`** (BYTEA, [ADR-036 §4](../../adr/ADR-036-workspaces-implementation.md)), **НЕ** через отложенный `attachments` ([TD-015](../../100-known-tech-debt.md)). Транспорт загрузки — **inline base64** (reuse классов/валидаций вложений [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)).

Лимиты ([ADR-036 §4](../../adr/ADR-036-workspaces-implementation.md)): `WORKSPACE_FILE_MAX_COUNT=20` файлов/workspace; `WORKSPACE_FILE_MAX_BYTES=8 MB`/файл; `WORKSPACE_FILES_TOTAL_BYTES=32 MB`/workspace. allowlist `mediaType` = `image/jpeg|png|gif|webp`, `application/pdf`, `text/plain|markdown|csv`, `application/json` ([Q-020-1](../../99-open-questions.md)). Вне списка → `422 unsupported_media_type`; превышение размера файла → `413`; превышение числа/суммарного размера → `422`.

## POST /v1/workspaces/{workspace_id}/files
Загрузить файл-знание (inline base64). При загрузке backend извлекает `extracted_text` (document/text) и сохраняет байты в `workspace_files.content`.
### Request
```json
{ "type": "image | document | text", "mediaType": "string", "filename": "string", "data": "base64" }
```
- `type`/`mediaType`/`filename`/`data` — те же поля и валидации, что у chat-вложений ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)). `filename` обязателен (используется в разметке контекста `[Файл проекта: {filename}]`).

### Response (201)
```json
{ "fileId": "uuid", "filename": "string", "mediaType": "string", "size": 0, "hasExtractedText": true, "createdAt": "ISO8601" }
```

## GET /v1/workspaces/{workspace_id}/files
Список файлов-знаний workspace (метаданные).
### Response (200)
```json
{
  "items": [
    { "fileId": "uuid", "filename": "string", "mediaType": "string", "size": 0, "hasExtractedText": true, "createdAt": "ISO8601" }
  ]
}
```

## DELETE /v1/workspaces/{workspace_id}/files/{file_id}
Удалить файл-знание (вместе с BYTEA). Идемпотентно: отсутствующий/чужой `file_id` → `404` (path-параметр URL `file_id`; в теле ответов поле — `fileId`, camelCase).
### Response (200)
```json
{ "deleted": true }
```

---

## Привязка чатов
- Чат привязывается к workspace при `POST /v1/chat/run` с `workspaceProjectId` (uuid, **session-fixed**, см. [chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md#workspaceprojectid-adr-036)). Валидируется принадлежность workspace пользователю при создании сессии: чужой/несуществующий → `404 workspace_not_found`. На resume берётся из сессии, поле запроса игнорируется.
- Список чатов workspace — `GET /v1/chats?workspaceProjectId={id}` (модуль [chats](../chats/02-api-contracts.md)).
