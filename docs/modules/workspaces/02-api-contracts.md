# Workspaces — API Contracts

JWT, владелец = `sub`. Чужой workspace → `404`.

## POST /v1/workspaces
### Request
```json
{ "name": "string", "description": "string (optional)", "instructions": "string (optional)" }
```
- `extra='forbid'`. `name` ≤ 120, `description` ≤ 1000, `instructions` ≤ 16000 символов.

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
Список workspace пользователя.
### Response (200)
```json
{ "items": [ { "id": "uuid", "name": "string", "description": "string | null", "updatedAt": "ISO8601", "fileCount": 0, "chatCount": 0 } ] }
```

## GET /v1/workspaces/{id}
Полный объект workspace (включая `instructions` и список файлов).
### Response (200)
```json
{
  "id": "uuid",
  "name": "string",
  "description": "string | null",
  "instructions": "string | null",
  "files": [ { "fileId": "uuid", "attachmentId": "uuid", "filename": "string | null", "mediaType": "string", "size": 0 } ],
  // files[] зависит от таблицы attachments — отложена на MVP (см. раздел «Файлы-контекст workspace — предпосылка»: BR-WS-3 / TD-015)
  "createdAt": "ISO8601",
  "updatedAt": "ISO8601"
}
```

## PATCH /v1/workspaces/{id}
Обновление `name`/`description`/`instructions` (любое подмножество).
### Request
```json
{ "name": "string", "description": "string", "instructions": "string" }
```
- `extra='forbid'`, те же лимиты длины. Хотя бы одно поле.
### Response (200)
Полный объект (как GET /{id}).

## DELETE /v1/workspaces/{id}
### Response (200)
```json
{ "deleted": true }
```
- Cascade удаляет `workspace_files`; `chat_sessions.workspace_project_id` → NULL (чаты остаются).

## Файлы-контекст workspace — предпосылка
> ⚠️ Контракты файлов-контекста ниже (`{attachmentId}` в POST/response, `files[]` в GET `/{id}`) зависят от таблицы `attachments` и двухшагового upload ([BR-WS-3](00-overview.md), [ADR-014](../../adr/ADR-014-multimodal-attachments.md)), **отложенных на MVP** (transport Superseded → [TD-015](../../100-known-tech-debt.md); chat-вложения MVP — inline base64, [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)). Модуль `workspaces` реализует таблицу `attachments` + upload как свою предпосылку Спринта 2.

## POST /v1/workspaces/{id}/files
Привязать ранее загруженный attachment как файл-контекст.
### Request
```json
{ "attachmentId": "uuid" }
```
- `attachmentId` обязан принадлежать `sub` (иначе `403`/`404`). Дубликат привязки → `409` (или идемпотентно `200`; дефолт `409` по `ux_workspace_files`).

### Response (201)
```json
{ "fileId": "uuid", "attachmentId": "uuid", "filename": "string | null", "mediaType": "string", "size": 0 }
```

## GET /v1/workspaces/{id}/files
Список файлов-контекста workspace.
### Response (200)
```json
{ "items": [ { "fileId": "uuid", "attachmentId": "uuid", "filename": "string | null", "mediaType": "string", "size": 0 } ] }
```

## DELETE /v1/workspaces/{id}/files/{fileId}
Отвязать файл от workspace (сам `attachment` не удаляется автоматически — принадлежит пользователю).
### Response (200)
```json
{ "deleted": true }
```

## Привязка чатов
- Чат привязывается к workspace при `POST /v1/chat/run` с `workspaceProjectId` (см. [chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md)). Фиксируется на сессию при создании.
- Список чатов workspace — `GET /v1/chats?workspaceProjectId={id}` (модуль [chats](../chats/02-api-contracts.md)).
