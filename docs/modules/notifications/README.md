# Module: Notifications

- Статус: **Реализован** (CRUD токена + APNs media-ready, [ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)). Остаток TD-011 — не-media триггеры.
- Ответственность: toggle (`user_preferences.notifications_enabled`) + регистрация APNs device-токена + отправка push при `media_jobs` → `completed`.

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

## Changelog
- 2026-08-11: реализация Phase 1–3 для media ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)).
- 2026-06-02: bootstrap модуля (architect, Figma-gap).
