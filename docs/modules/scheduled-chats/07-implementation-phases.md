# Scheduled Chats — Implementation Phases

Честный статус на 2026-09-18 (состояния разведены): **код фаз 1–5 в дереве есть**; **автотесты в дереве** (`tests/unit/test_scheduled_chats_adr107.py`, `tests/integration/test_scheduled_chats_adr107.py`; OpenAPI tag; qa 21 passed — по сообщению orchestrator'а); **backend-reviewer `approve` (resume_only)**; **выкатка на инстансы не утверждается** (не измерялась).

1. **Phase 0 — docs** ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md) + этот модуль). ✅ (2026-09-18)
2. **Phase 1 — миграция + модели:** таблица `scheduled_chat_tasks`, индексы; ORM/repository. ✅ (`migrations/versions/20260918_0036_scheduled_chat_tasks.py`, revision `0036_scheduled_chat_tasks`; `src/app/scheduled_chats/`)
3. **Phase 2 — HTTP CRUD:** роутер, схемы, валидации лимитов. ✅ (`src/app/api_gateway/routers/scheduled_chats.py`, `src/app/schemas/scheduled_chats.py`). Тесты контракта — ✅ `tests/unit|integration/test_scheduled_chats_adr107.py`.
4. **Phase 3 — worker:** lifespan poller, claim `SKIP LOCKED`, stuck-TTL recovery (`worker_interrupted`), preflight session, вызов `ChatOrchestrator.run` (v2) + маппинг `ChatResponse`. ✅ (`src/app/scheduled_chats/worker.py`)
5. **Phase 4 — push:** `notify_scheduled_chat_ready` + payload `type=scheduled_chat_ready` (`sessionId` = result ?? planned ?? null); идемпотентность `push_sent_at`. ✅ (`src/app/notifications/push_service.py`, `apns_client.py`)
6. **Phase 5 — ops:** env в `.env*.example` / deploy checklist (вкл. `SCHEDULED_CHAT_RUNNING_TTL_SECONDS`). ✅ (ключи в `.env.example` / `.env.prod.example`; checklist — [07-deployment.md](../../07-deployment.md)). `POLL_SECONDS=0` в тестах + покрытие — ✅ (в тех же ADR-107 тест-файлах).

Вне фаз MVP: recurring; Celery; TD-010/TD-013 cleanup.
