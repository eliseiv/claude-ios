# ADR-107 — Запланированные чат-задачи (отложенный запуск чата)

- **Статус:** Accepted. **Реализация (честный статус на 2026-09-18; состояния разведены):** код **НАПИСАН** (миграция `migrations/versions/20260918_0036_scheduled_chat_tasks.py`, revision `0036_scheduled_chat_tasks`, пакет `src/app/scheduled_chats/`, роутер `scheduled_chats`, worker, push `scheduled_chat_ready`); **автотесты В ДЕРЕВЕ** (`tests/unit/test_scheduled_chats_adr107.py`, `tests/integration/test_scheduled_chats_adr107.py`; OpenAPI tag `ScheduledChats` в `tests/integration/test_api_documentation.py`; по сообщению orchestrator'а — qa: 21 passed на этих файлах); **backend-reviewer: `approve` (resume_only)** — по сообщению orchestrator'а; **выкатка на инстансы НЕ утверждается** (не измерялась).
- **Дата:** 2026-09-18
- **Тип:** feature-ADR (MVP).
- **Связано:** [ADR-067](ADR-067-media-ready-push-and-reconciler.md) (образец in-process poller + APNs), [ADR-032](ADR-032-notifications-enabled-default-false.md) (toggle), [ADR-012](ADR-012-assistant-mode-vs-billing-mode.md) (`mode` = billing), [ADR-064](ADR-064-study-learn-quiz-generation-mode.md) (`generationMode`, v2), [ADR-104](ADR-104-voice-mode-websocket.md) (внутренний вызов `ChatOrchestrator.run` без JWT на границе воркера), [TD-011](../100-known-tech-debt.md), [TD-010](../100-known-tech-debt.md), [TD-013](../100-known-tech-debt.md).
- **Реализуется в:** [modules/scheduled-chats](../modules/scheduled-chats/README.md), [modules/notifications](../modules/notifications/README.md) (новый push-триггер).

## Контекст

Пользователь хочет задать текст и время «напомни / напиши чату позже», свернуть приложение и получить push, когда сервер сам выполнит ход. Клиентский таймер на iOS ненадёжен (фон ~30 с, как у media). Нужна серверная одноразовая очередь.

## Решение владельца (не пересматривается)

- **Одноразовая** задача: `prompt` + `runAt` (+ опциональный `sessionId`).
- В срок сервер сам вызывает **`ChatOrchestrator.run` (v2)** и шлёт **APNs** с deep-link в чат.
- **Вне MVP:** recurring, Celery/ARQ, cleanup [TD-010](../100-known-tech-debt.md)/[TD-013](../100-known-tech-debt.md) как отдельные follow-up.

## Решение

### 1. Исполнение — внутренний `ChatOrchestrator.run` (v2), без JWT

Воркер (in-process loop) вызывает оркестратор **напрямую** с `user_id` владельца задачи — тот же класс внутреннего вызова, что голосовой WS ([ADR-104](ADR-104-voice-mode-websocket.md)): граница JWT — HTTP CRUD; исполнение — доверенный серверный контур.

Контракт хода — **v2** (`generationMode` из задачи или дефолт `general`). Биллинг/policy/wallet — **обычные** на момент запуска (не «бронь» при создании). Отказ policy/wallet/upstream → статус `failed`, `errorCode`/`errorMessage`, push о неудаче.

**Планируемая сессия (`session_id`):**

- `session_id = NULL` → при запуске создаётся **новая** сессия (как `/v1/chat/v2/run` без `sessionId`).
- `session_id ≠ NULL` → **только** resume этой owned-сессии. UUID **хранится как есть** (колонка `UUID`, без `ON DELETE SET NULL`): молчаливая подмена resume → new **запрещена**.
- FK на `chat_sessions` **не ставится** (как у ряда мягких ссылок в репо, где SET NULL ломает семантику): удаление чата не затирает UUID в задаче. Перед `run` и на create/PATCH: строка сессии обязана существовать и принадлежать владельцу; иначе → `failed` / HTTP `404` с `session_not_found`.
- Если к моменту запуска строки сессии нет (чат удалён) — задача → `failed` + `errorCode=session_not_found` + push; **не** создавать новую сессию.

Маппинг исхода оркестратора → статус задачи — [modules/scheduled-chats/03-architecture.md](../modules/scheduled-chats/03-architecture.md) (таблица `ChatResponse.status`).

### 2. Очередь — Postgres due-rows + in-process loop (не Celery)

Образец media reconciler ([ADR-067](ADR-067-media-ready-push-and-reconciler.md)): asyncio-loop в lifespan API.

- Env: `SCHEDULED_CHAT_POLL_SECONDS` (дефолт **15**; **`≤0` = воркер выключен**), `SCHEDULED_CHAT_BATCH_SIZE` (дефолт **10**), `SCHEDULED_CHAT_RUNNING_TTL_SECONDS` (дефолт **900**).
- Выборка due: `status = 'scheduled' AND run_at <= now()` ORDER BY `run_at`, LIMIT batch.
- **Claim (одно определение):** атомарный переход `scheduled → running` одним SQL `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING …` с записью `claimed_at = started_at = now()`. Повторный claim уже `running` **запрещён** (нет safe re-claim после старта исполнения).
- После `run`: `completed` + `result_session_id` / `result_message_step_id`, либо `failed` + error fields; затем push (см. §4).

**Stuck `running` (crash / kill воркера):** на каждом тике poller'а (тот же loop) — recovery:

- условие: `status = 'running' AND coalesce(started_at, claimed_at) < now() - RUNNING_TTL`;
- действие: `running → failed`, `error_code = worker_interrupted`, `finished_at = now()`, затем push (как у обычного fail);
- **не** возвращать в `scheduled` и **не** делать повторный claim той же строки после `started_at` (риск двойного хода / двойного списания).

### 3. Биллинг / `mode` (session-fixed)

`mode` (`credits` | `byok`) — **session-fixed**, как у чата ([ADR-012](ADR-012-assistant-mode-vs-billing-mode.md), оркестратор: на resume поле запроса игнорируется).

- **Новая сессия** (`sessionId` отсутствует / `null`): `mode` берётся из тела (дефолт `credits`), пишется в задачу и передаётся в `run`.
- **Resume** (`sessionId` задан) на **create / PATCH**: поле `mode` в теле **игнорируется** (не `422`); в задачу пишется `mode` существующей сессии. На запуске в оркестратор уходит billing сессии; сохранённый `mode` задачи с сессией не конфликтует.
- Кредиты **не** списываются при постановке. В момент запуска — обычный путь wallet/policy. Отказ → `failed` + push.

### 4. Push — новый триггер `scheduled_chat_ready`

**Не** переиспользовать `notify_media_ready`. Новый метод/триггер в notifications:

- custom key **`type`: `scheduled_chat_ready`**;
- поля: `scheduledChatId`, `sessionId`, `messageStepId` (nullable при failed до создания хода), `status` (`completed`|`failed`), `errorCode` (nullable);
- **`sessionId` в payload** = `resultSessionId ?? sessionId(planned) ?? null` (camelCase ответа задачи);
- deep-link в чат по `sessionId`, если не `null`; если `sessionId` в payload `null` — клиентский fallback: экран списка / деталь задачи по `scheduledChatId` (без открытия несуществующего чата);
- уважать `notifications_enabled`; без `APNS_*` — no-op;
- идемпотентность: `scheduled_chat_tasks.push_sent_at` (`UPDATE … WHERE push_sent_at IS NULL`);
- push и при **`failed`**, и при **`completed`** (включая recovery `worker_interrupted`).

### 5. HTTP API

CRUD JWT-скоуп владельца: `POST` / `GET` / `GET/{id}` / `PATCH` / `DELETE` `/v1/scheduled-chats`. Полный контракт, лимиты и коды — [modules/scheduled-chats/02-api-contracts.md](../modules/scheduled-chats/02-api-contracts.md).

Статусы: `scheduled` → `running` → `completed` | `failed` | `cancelled`. Cancel / PATCH — **только** из `scheduled`.

### 6. Данные

Таблица `scheduled_chat_tasks` — [modules/scheduled-chats/04-data-model.md](../modules/scheduled-chats/04-data-model.md), сводка в [03-data-model.md](../03-data-model.md). Миграция в дереве: `migrations/versions/20260918_0036_scheduled_chat_tasks.py` (revision `0036_scheduled_chat_tasks`, `down_revision=0035_media_features`; single head).

## Отклонённое

- **Celery / ARQ / Redis-очередь** — вне MVP; Postgres + lifespan достаточно (как media).
- **Recurring / cron-выражения** — вне MVP.
- **Списание кредитов при создании** — ломает UX «запланировал на завтра, а баланс изменится»; биллинг в момент запуска.
- **Переиспользование media push payload** — другой deep-link и семантика; отдельный `type`.
- **Закрытие TD-010/TD-013 этим воркером** — poller появляется, но orphan-attachments / refresh-token cleanup остаются отдельными задачами.
- **`ON DELETE SET NULL` на `session_id`** — молча превращает resume в «новую сессию»; отвергнуто в пользу хранения UUID + явного `session_not_found`.
- **Safe re-claim `running → scheduled`** после `started_at` — риск двойного исполнения; вместо этого TTL → `failed`/`worker_interrupted`.

## Последствия

- Частично закрывает остаток [TD-011](../100-known-tech-debt.md) (ещё один event-триггер помимо media).
- Инфраструктура in-process poller **не** закрывает [TD-010](../100-known-tech-debt.md)/[TD-013](../100-known-tech-debt.md): cleanup всё ещё отдельная задача.
- Multi-worker безопасен через `SKIP LOCKED` + `push_sent_at`; stuck-running закрывается TTL-recovery.
- Per-instance: те же `APNS_*`, что для media; плюс `SCHEDULED_CHAT_*`.
- Код/миграции/`.env.example` — зона backend/devops (код и автотесты в дереве есть; выкат — см. строку **Статус**).

## Ревизии

| Дата | Суть |
|---|---|
| 2026-09-18 | Уточнение **факта статуса реализации** (решения §1–§6 не пересматривались): автотесты **в дереве** (`tests/unit/test_scheduled_chats_adr107.py`, `tests/integration/test_scheduled_chats_adr107.py`; OpenAPI tag `ScheduledChats` в `test_api_documentation.py`; qa 21 passed — по сообщению orchestrator'а); `backend-reviewer` approve resume_only; выкат на инстансы не утверждается. |
| 2026-09-18 | Уточнение **факта статуса реализации** (решения §1–§6 не пересматривались): код написан; автотесты пишет qa; `backend-reviewer` approve resume_only (по сообщению orchestrator'а); выкат на инстансы не утверждается. |
| 2026-09-18 | Уточнение контракта (не reversal решения владельца): stuck-`running` TTL → `worker_interrupted`; `session_id` без SET NULL + явный fail; `mode` session-fixed на resume; формула push `sessionId` + маппинг `ChatResponse.status`. |
