# Notifications — API Contracts

JWT, владелец = `sub`. Deep link media-ready push — по `jobId` (не `conversation_id` / apphud).

## POST /v1/notifications/device-token
Регистрация/обновление APNs device-токена.

### Request
```json
{
  "deviceId": "string (optional — иначе из JWT claim / X-Device-Id)",
  "pushToken": "string (APNs device token)",
  "platform": "ios"
}
```
- `extra='forbid'`. `pushToken` ≤ 512 символов. `deviceId` — если не передан, берётся из JWT/`X-Device-Id`; если и там нет → `422`.
- Upsert по `(user_id, device_id)`.

### Response (200)
```json
{ "registered": true }
```

## DELETE /v1/notifications/device-token
Удалить токен устройства (отписка / logout).
### Request
```json
{ "deviceId": "string (optional — иначе из JWT/X-Device-Id)" }
```
### Response (200)
```json
{ "deleted": true }
```

## Настройка уведомлений (toggle)
- `notificationsEnabled` — через [preferences](../preferences/02-api-contracts.md): `GET`/`PATCH /v1/preferences`.

## Исходящий push (APNs, media ready) — [ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)

Не HTTP API. Клиент получает APNs notification:

```json
{
  "aps": {
    "alert": { "title": "Ready", "body": "Your video is ready" },
    "mutable-content": 1,
    "sound": "default"
  },
  "jobId": "<uuid>",
  "kind": "image|video",
  "mediaUrl": "https://…"
}
```

- `mediaUrl` — тот же URL, что `assets[0].url` в `GET /v1/media/jobs/{jobId}`.
- Отправка только при `completed`, один раз (`push_sent_at`), если `notificationsEnabled` и есть токен и настроены `APNS_*`.

## Исходящий push (APNs, scheduled chat ready) — [ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)

Не HTTP API. **Не** переиспользовать media payload / `notify_media_ready`. Полный контракт — [scheduled-chats/02-api-contracts.md](../scheduled-chats/02-api-contracts.md). Кратко:

```json
{
  "aps": {
    "alert": { "title": "Chat ready", "body": "Your scheduled chat has finished" },
    "sound": "default"
  },
  "type": "scheduled_chat_ready",
  "scheduledChatId": "<uuid>",
  "sessionId": "<uuid|null>",
  "messageStepId": "<uuid|null>",
  "status": "completed|failed",
  "errorCode": "string|null"
}
```

- **`sessionId`** = `resultSessionId ?? planned sessionId ?? null`.
- Deep-link — по `sessionId`, если не `null`; иначе fallback UI по `scheduledChatId`.
- Отправка при `completed` **и** `failed` (включая `worker_interrupted`), один раз (`scheduled_chat_tasks.push_sent_at`).
- Код триггера ещё не написан (docs-only на 2026-09-18).
