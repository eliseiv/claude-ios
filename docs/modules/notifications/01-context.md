# Notifications — Context

## Зависимости
- **API Gateway** — auth, provisioning, `device_id` из claim/`X-Device-Id`, роуты `/v1/notifications/*`.
- **preferences** — `notifications_enabled` (единый источник настройки). Notifications-модуль не дублирует toggle.
- **device_push_tokens** таблица.
- **APNs** — token-based JWT auth, env `APNS_*` ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)).
- **media-generation** — триггер `notify_media_ready` после `completed`.
- **scheduled-chats** — триггер `scheduled_chat_ready` после `completed`/`failed` ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md); код написан, автотесты в дереве).

## Границы
- Модуль хранит токены и отправляет push по явным триггерам доменных модулей; не планирует задачи сам (poller — у media / scheduled-chats).
