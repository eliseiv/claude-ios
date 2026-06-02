# Attachments — API Contracts

JWT, владелец = `sub`.

## POST /v1/attachments
Загрузка вложения (отдельным транспортом, **до** `/chat/run`).

### Request
- `Content-Type: multipart/form-data`.
- Поля: `file` (бинарь, обязателен), `kind` (`image | document`, опц. — иначе выводится из media_type).
- Transport-лимит тела: image ≤ 5 MB, document ≤ 10 MB ([Q-014-2](../../99-open-questions.md)). Превышение → `413`.

### Поведение
- Определить/проверить `media_type` по содержимому (magic bytes), не доверяя расширению/заголовку клиента; вне allowlist → `422` ([05-security.md](05-security.md)).
- Для `document` (PDF/text) — извлечь `extracted_text` (усечение до лимита контекста).
- Сохранить в `attachments` (`user_id=sub`, `session_id=NULL`).

### Response (201)
```json
{
  "id": "uuid",
  "kind": "image | document",
  "mediaType": "image/png",
  "filename": "string | null",
  "size": 12345,
  "hasExtractedText": true,
  "createdAt": "ISO8601"
}
```
- Сырой бинарь и `extracted_text` наружу не возвращаются (используются backend'ом при сборке Anthropic-запроса).

## GET /v1/attachments/{id}
Метаданные вложения владельца.
### Response (200)
Та же схема, что POST-ответ. Чужое/несуществующее → `404`.

## DELETE /v1/attachments/{id}
### Response (200)
```json
{ "deleted": true }
```
- Если вложение используется `workspace_files` — удаление каскадно убирает связь (`workspace_files` FK `ON DELETE CASCADE`); `attachments.session_id` references — независимы.

## Использование в /chat/run (chat-orchestrator)
- В теле `/chat/run`: `attachments: [{ "id": "uuid" }]` (≤ 10). См. [chat-orchestrator/02-api-contracts.md](../chat-orchestrator/02-api-contracts.md).
- Orchestrator: проверка владельца (`attachments.user_id == sub`, иначе `403`/`404`), сборка content-блоков:
  - `kind=image` → Anthropic `image` block (base64, media_type из записи).
  - `kind=document` → Anthropic `document` block (PDF) **или** текстовый блок из `extracted_text` (по типу).
- Проставление `attachments.session_id` при первом использовании (для истории чата).
