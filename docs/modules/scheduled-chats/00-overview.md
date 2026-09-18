# Scheduled Chats — Overview

## Назначение
Одноразовые «запланированные чат-задачи»: пользователь задаёт текст и время запуска; в срок сервер сам выполняет ход чата (v2) и шлёт APNs с deep-link в сессию ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)).

## Scope (MVP)
- CRUD `/v1/scheduled-chats` (JWT, владелец = `sub`).
- Статусы: `scheduled` → `running` → `completed` | `failed` | `cancelled`.
- In-process poller (Postgres due-rows), claim через `FOR UPDATE SKIP LOCKED`.
- Исполнение: внутренний `ChatOrchestrator.run` (v2), без JWT на границе воркера.
- Push: новый триггер `scheduled_chat_ready` (не media).

## Out of scope
- Recurring / cron-расписания.
- Celery / ARQ / внешняя очередь.
- Cleanup orphan-attachments ([TD-010](../../100-known-tech-debt.md)) и refresh-токенов ([TD-013](../../100-known-tech-debt.md)).
- Вложения / edit / tool-result в момент планирования (только текст `prompt`).

## Бизнес-правила
- BR-SC-1: задача одноразовая; после терминального статуса повторного запуска нет.
- BR-SC-2: `runAt` tz-aware и строго в будущем (с мин. lead и макс. горизонтом — см. API); naive → `422`.
- BR-SC-3: лимит активных (`scheduled`+`running`) на пользователя.
- BR-SC-4: cancel и PATCH — только из `scheduled`.
- BR-SC-5: кредиты списываются (если нужны) **в момент запуска**, не при создании.
- BR-SC-6: `sessionId = null` → новая сессия; иначе **только** resume того UUID. Отсутствие строки сессии на run → `failed`/`session_not_found`; молчаливая подмена resume→new **запрещена**.
- BR-SC-7: `mode` session-fixed; при create/PATCH с `sessionId` поле `mode` в теле игнорируется (пишется mode сессии).
- BR-SC-8: `running` старше `SCHEDULED_CHAT_RUNNING_TTL_SECONDS` → `failed`/`worker_interrupted` + push (без re-claim).
