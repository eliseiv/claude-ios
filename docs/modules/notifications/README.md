# Module: Notifications

- Статус: **Реализован** (CRUD токена + APNs media-ready, [ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)). Триггер `scheduled_chat_ready` — [ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md) (**код написан**; автотесты в дереве; выкат не утверждается). Остаток TD-011 — прочие не-media/не-scheduled триггеры.
- Ответственность: toggle (`user_preferences.notifications_enabled`) + регистрация APNs device-токена + отправка push при `media_jobs` → `completed` и при завершении scheduled-chat.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `device_push_tokens` (таблица 17, миграция `0022`); настройка — `user_preferences.notifications_enabled`.

## DoD
- `POST /v1/notifications/device-token`, `DELETE /v1/notifications/device-token`.
- Toggle — `PATCH /v1/preferences` (`notificationsEnabled`).
- APNs media-ready push: `jobId` + `kind` + `mediaUrl` + `aps.mutable-content=1` ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)).
- Фоновый media reconciler — чтобы push ушёл без клиентского poll.
- APNs scheduled-chat push: `type=scheduled_chat_ready` + `sessionId` ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)) — код написан; автотесты в дереве; выкат не утверждается.

## Changelog
- 2026-09-18: статус триггера `scheduled_chat_ready` — код написан ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)); автотесты в дереве (`test_scheduled_chats_adr107`; qa 21 passed — по сообщению orchestrator'а); выкат не утверждается.
- 2026-09-18: спроектирован триггер `scheduled_chat_ready` ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)).
- 2026-08-11: реализация Phase 1–3 для media ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)).
- 2026-06-02: bootstrap модуля (architect, Figma-gap).
