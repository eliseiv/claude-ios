# Notifications — Implementation Phases

1. **Phase 1 — миграция:** `device_push_tokens` + `media_jobs.push_sent_at` (`0022_device_push_tokens`). ✅
2. **Phase 2 — token CRUD:** `POST`/`DELETE /v1/notifications/device-token`. ✅
3. **Phase 3 — отправка + reconciler:** APNs-клиент, media-ready trigger, lifespan reconciler ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)). ✅
4. **Phase 4 — scheduled-chat push:** триггер `scheduled_chat_ready` ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)) — ✅ код написан; ✅ автотесты в дереве (`test_scheduled_chats_adr107`); выкат не утверждается.

Остаток [TD-011](../../100-known-tech-debt.md): прочие не-media/не-scheduled триггеры / hardening.
