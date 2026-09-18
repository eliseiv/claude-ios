# Module: Scheduled Chats (запланированные чат-задачи)

- Статус: **код написан** ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)) — миграция `0036`, `src/app/scheduled_chats/`, router, worker, push. **Автотесты в дереве** (`tests/unit/test_scheduled_chats_adr107.py`, `tests/integration/test_scheduled_chats_adr107.py`; OpenAPI tag `ScheduledChats`; qa 21 passed — по сообщению orchestrator'а). **Ревью:** `backend-reviewer` → `approve` (resume_only). **Выкатка на инстансы не утверждается** (не измерялась). Честный статус на 2026-09-18.
- Ответственность: одноразовая отложенная постановка хода чата (prompt + `runAt`), серверное исполнение через `ChatOrchestrator.run` (v2) и APNs deep-link в чат.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [04-data-model.md](04-data-model.md)
- [05-security.md](05-security.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

## DoD
- CRUD `/v1/scheduled-chats` по [02-api-contracts.md](02-api-contracts.md).
- In-process worker + `FOR UPDATE SKIP LOCKED` claim.
- Исполнение v2-оркестратором; push `type=scheduled_chat_ready`.
- Миграция таблицы `scheduled_chat_tasks` (single head) — `0034`.
- Env `SCHEDULED_CHAT_*` в config + deployment docs.
- ✅ Автотесты (контракт / worker / push) — `tests/unit/test_scheduled_chats_adr107.py`, `tests/integration/test_scheduled_chats_adr107.py` (qa 21 passed — по сообщению orchestrator'а).
- ⏳ Выкат на инстансы — не утверждён (не измерялся).

## Changelog
- 2026-09-18: статус — код написан; автотесты в дереве (unit+integration + OpenAPI tag; qa 21 passed); ревью approve (resume_only); выкат не утверждается.
- 2026-09-18: модуль спроектирован — [ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md).
