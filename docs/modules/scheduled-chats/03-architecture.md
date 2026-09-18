# Scheduled Chats — Architecture

## Размещение

Пакет `src/app/scheduled_chats/`: `repository`, `service`, `worker` (poller). Роутер `src/app/api_gateway/routers/scheduled_chats.py`. Схемы `src/app/schemas/scheduled_chats.py`. Старт воркера — lifespan API рядом с media reconciler.

## Поток

```
POST /v1/scheduled-chats  →  INSERT status=scheduled
        │
        ▼  (poll every SCHEDULED_CHAT_POLL_SECONDS, if > 0)
   ┌────┴────┐
   │ due     │  status=scheduled AND run_at<=now()
   │ claim   │  → running (claimed_at, started_at)
   └────┬────┘
        │
        ▼
   preflight session (если session_id задан)
        │  нет строки / чужой → failed/session_not_found + push
        ▼
ChatOrchestrator.run(v2)  — user_id владельца, без JWT
        │
        ├─ map ChatResponse → completed | failed  (таблица ниже)
        └─ push notify_scheduled_chat_ready
        │
паралельно на каждом тике:
   stuck running (TTL) → failed/worker_interrupted + push
```

## Claim (одно определение)

**Claim** = атомарный переход `scheduled → running` в одной транзакции:

1. `SELECT id FROM scheduled_chat_tasks WHERE status='scheduled' AND run_at <= now() ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT :batch`
2. `UPDATE … SET status='running', claimed_at=now(), started_at=now() WHERE id = ANY(…) AND status='scheduled' RETURNING *`

Второй воркер не получит те же строки. Повторный claim уже `running` **запрещён**. Safe re-claim `running → scheduled` после записи `started_at` **не вводится** (риск двойного хода).

## Stuck `running` — recovery

На каждом тике того же poller'а (до или после due-claim — порядок реализации свободен, оба шага обязательны):

| Условие | Действие |
|---|---|
| `status='running'` и `coalesce(started_at, claimed_at) < now() - SCHEDULED_CHAT_RUNNING_TTL_SECONDS` | `UPDATE … SET status='failed', error_code='worker_interrupted', finished_at=now()` (только если ещё `running`), затем push |

Дефолт TTL — **900** с. Crash процесса / kill mid-run не оставляет задачу вечно в `running`.

## Preflight сессии

Если `session_id IS NOT NULL`:

1. Загрузить `chat_sessions` по id + `user_id` владельца задачи.
2. Нет строки / чужой → **не** вызывать оркестратор; `failed` + `error_code=session_not_found` + push.
3. **Запрещено** трактовать отсутствие сессии как «создать новую» (запрет молчаливой подмены resume→new, [ADR-107 §1](../../adr/ADR-107-scheduled-chat-tasks.md)).

## Исполнение хода

Сборка запроса к оркестратору (нормативно):

- `message` = `prompt` задачи;
- `sessionId` = сохранённый UUID или `None` (новая сессия);
- `mode` — при новой сессии: из задачи; при resume: **session-fixed** (как `/chat/v2/run` — поле запроса игнорируется оркестратором; в задачу на create/PATCH с `sessionId` пишется `mode` сессии);
- `assistantMode` / `model` — только если создаётся новая сессия; при resume игнорируются;
- `generationMode` = сохранённый или `general`;
- attachments / edit / context — **не** передаются в MVP.

Policy/wallet — штатные гейты v2.

### Маппинг `ChatResponse` → статус задачи

| `ChatResponse.status` | `blockReason` / условие | Статус задачи | `errorCode` |
|---|---|---|---|
| `assistant_message` | — | `completed` | `null` |
| `blocked` | policy / wallet (`blockReason` ≠ `max_tokens`) | `failed` | строка `blockReason` (напр. `credits_empty`, `byok_invalid`, …) |
| `blocked` | `max_tokens` | `failed` | `max_tokens` |
| `tool_call` | первый ответ — client-side tool hand-off | `failed` | `tool_loop_unsupported` |
| исключение / 5xx-класс оркестратора | — | `failed` | доменный код или `upstream_error` |

Server-side tools внутри оркестратора (до финального `assistant_message`) допустимы и не меняют строку `tool_call` выше — строка про **client-side** hand-off без устройства.

При `completed`: `result_session_id` / `result_message_step_id` из ответа. При `failed` до создания хода — оба могут остаться `null`.

## Push

Новый метод notifications (имя реализации на усмотрение backend, контракт payload — [02-api-contracts.md](02-api-contracts.md)):

- **не** `notify_media_ready`;
- идемпотентный claim `push_sent_at`;
- `410` APNs → удаление токена (как ADR-067);
- **`sessionId` в payload** = `result_session_id ?? session_id ?? null`;
- если итоговый `sessionId` `null` — deep-link fallback по `scheduledChatId` (список/деталь задачи).

## Env

| Key | Default | Notes |
|---|---|---|
| `SCHEDULED_CHAT_POLL_SECONDS` | `15` | `≤0` = off |
| `SCHEDULED_CHAT_BATCH_SIZE` | `10` | |
| `SCHEDULED_CHAT_RUNNING_TTL_SECONDS` | `900` | stuck-`running` → `worker_interrupted` |
| `SCHEDULED_CHAT_MAX_ACTIVE_PER_USER` | `20` | |
| `SCHEDULED_CHAT_PROMPT_MAX_CHARS` | `32768` | |
| `SCHEDULED_CHAT_MIN_LEAD_SECONDS` | `60` | |
| `SCHEDULED_CHAT_MAX_LEAD_DAYS` | `90` | |

APNs — существующие `APNS_*` ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)).

## Инварианты

- Одна задача — один запуск (нет re-claim после `started_at`).
- Владелец строки = `user_id`; воркер не повышает привилегии.
- Prompt в структурные логи не кладётся целиком (redaction / truncate).
- Resume с отсутствующей сессией → `failed`/`session_not_found`, не новая сессия.
