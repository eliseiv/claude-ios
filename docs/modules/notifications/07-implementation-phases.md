# Notifications — Implementation Phases

1. **Phase 1 — миграция:** `device_push_tokens` + `media_jobs.push_sent_at` (`0022_device_push_tokens`). ✅
2. **Phase 2 — token CRUD:** `POST`/`DELETE /v1/notifications/device-token`. ✅
3. **Phase 3 — отправка + reconciler:** APNs-клиент, media-ready trigger, lifespan reconciler ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)). ✅

Остаток [TD-011](../../100-known-tech-debt.md): не-media триггеры / hardening.
