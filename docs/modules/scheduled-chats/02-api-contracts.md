# Scheduled Chats — API Contracts

Все эндпоинты требуют `Authorization: Bearer <accessToken>`. Владелец = JWT `sub`. Чужой/несуществующий id → `404`. Тег OpenAPI — `ScheduledChats`. Тела — `StrictModel` (`extra=forbid`): лишнее поле → `422`. Rate-limit — `enforce_other_limits` (как notifications/media CRUD).

Нормативное решение: [ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md).

---

## Лимиты (конкретные числа)

| Параметр | Env / константа | Дефолт | Смысл |
|---|---|---|---|
| Макс. длина `prompt` | `SCHEDULED_CHAT_PROMPT_MAX_CHARS` | **32768** (32 KiB, как `SIZE_LIMIT_MESSAGE`) | иначе `422 prompt_too_long` |
| Мин. lead `runAt` | `SCHEDULED_CHAT_MIN_LEAD_SECONDS` | **60** | `runAt` ≥ `now() + 60s`, иначе `422 run_at_too_soon` |
| Макс. горизонт | `SCHEDULED_CHAT_MAX_LEAD_DAYS` | **90** | `runAt` ≤ `now() + 90d`, иначе `422 run_at_too_far` |
| Активных задач / user | `SCHEDULED_CHAT_MAX_ACTIVE_PER_USER` | **20** | `status ∈ {scheduled, running}`; иначе `409 active_limit_exceeded` |
| Интервал poller | `SCHEDULED_CHAT_POLL_SECONDS` | **15** | `≤0` → воркер **выключен** |
| Batch claim | `SCHEDULED_CHAT_BATCH_SIZE` | **10** | строк за тик |
| Stuck-running TTL | `SCHEDULED_CHAT_RUNNING_TTL_SECONDS` | **900** | `running` старше TTL → `failed`/`worker_interrupted` |

`runAt` обязан быть в будущем относительно серверного UTC (`now()`): даже при lead ≥ 60 с проверка `runAt > now()` остаётся.

Список `limit` query: дефолт **30**, max **100** (как snippets/chats).

---

## Объект ответа `ScheduledChat`

```json
{
  "id": "uuid",
  "sessionId": "uuid | null",
  "prompt": "string",
  "mode": "credits | byok",
  "assistantMode": "chat | code | null",
  "model": "string | null",
  "generationMode": "general | research | reasoning | study_learn | null",
  "runAt": "ISO8601",
  "status": "scheduled | running | completed | failed | cancelled",
  "resultSessionId": "uuid | null",
  "resultMessageStepId": "uuid | null",
  "errorCode": "string | null",
  "errorMessage": "string | null",
  "claimedAt": "ISO8601 | null",
  "startedAt": "ISO8601 | null",
  "finishedAt": "ISO8601 | null",
  "pushSentAt": "ISO8601 | null",
  "createdAt": "ISO8601",
  "updatedAt": "ISO8601"
}
```

| Поле | Смысл |
|---|---|
| `sessionId` | целевая сессия на момент планирования (`null` = создать новую при запуске). UUID хранится даже если чат позже удалён |
| `mode` | billing_mode ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)). **Session-fixed:** при create/PATCH **без** `sessionId` — из тела (дефолт `credits`); **с** `sessionId` — пишется `mode` сессии, поле тела **игнорируется** |
| `assistantMode` / `model` | session-fixed для **новой** сессии; при resume на запуске **игнорируются** (как `/chat/v2/run`) |
| `generationMode` | per-turn на запуске; `null`/опущено → **`general`** |
| `resultSessionId` | сессия, в которой фактически выполнен ход (`null` до завершения / при fail до создания) |
| `resultMessageStepId` | `messageStepId` хода ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)); `null` если ход не создан |
| `errorCode` / `errorMessage` | машинный код + короткий текст при `failed` (без секретов) |

---

## `POST /v1/scheduled-chats`

Создать задачу. Статус сразу `scheduled`. Кредиты **не** списываются.

### Request

```json
{
  "prompt": "string (required, non-empty after strip)",
  "runAt": "ISO8601 (required, tz-aware)",
  "sessionId": "uuid (optional)",
  "mode": "credits | byok (optional, default credits; ignored if sessionId set)",
  "assistantMode": "chat | code (optional)",
  "model": "string (optional)",
  "generationMode": "general | research | reasoning | study_learn (optional)"
}
```

### Валидация

| Условие | HTTP | `error.code` |
|---|---|---|
| пустой `prompt` после strip | 422 | `prompt_required` |
| `len(prompt) > SCHEDULED_CHAT_PROMPT_MAX_CHARS` | 422 | `prompt_too_long` |
| `runAt` без timezone (naive) | 422 | `run_at_timezone_required` |
| `runAt` ≤ `now()` | 422 | `run_at_not_in_future` |
| `runAt` < `now() + MIN_LEAD` | 422 | `run_at_too_soon` |
| `runAt` > `now() + MAX_LEAD` | 422 | `run_at_too_far` |
| `mode` вне `credits`\|`byok` (когда поле применяется — без `sessionId`) | 422 | `unsupported_mode` |
| `assistantMode` вне `chat`\|`code` | 422 | `unsupported_assistant_mode` |
| `generationMode` вне набора | 422 | `unsupported_generation_mode` |
| `model` вне allowlist инстанса (если передан) | 422 | `unsupported_model` |
| `sessionId` задан, но чужой/нет | 404 | `session_not_found` |
| число активных ≥ max | 409 | `active_limit_exceeded` |
| нет JWT | 401 | `unauthorized` |
| rate limit | 429 | `rate_limited` |

**`mode` + `sessionId`:** если `sessionId` задан и сессия найдена — значение `mode` из тела **игнорируется**; в строку пишется `chat_sessions.mode`. Mismatch с телом **не** даёт `422` (выравнивание с `/chat/v2/run`).

### Response `201`

Полный `ScheduledChat` (`status: "scheduled"`).

---

## `GET /v1/scheduled-chats`

Список задач владельца, newest-first по `created_at`.

### Query

| Параметр | Смысл |
|---|---|
| `status` | опц. фильтр: одно из `scheduled`\|`running`\|`completed`\|`failed`\|`cancelled` |
| `cursor` | opaque (из `nextCursor`) |
| `limit` | 1…100, дефолт 30 |

### Response `200`

```json
{
  "items": [ { /* ScheduledChat */ } ],
  "nextCursor": "string | null"
}
```

---

## `GET /v1/scheduled-chats/{id}`

### Response `200`

`ScheduledChat`. Чужой/нет → `404` (`scheduled_chat_not_found`).

---

## `PATCH /v1/scheduled-chats/{id}`

Обновить поля **только** пока `status = scheduled`. Иначе `409 not_patchable`.

### Request (любое подмножество)

```json
{
  "prompt": "string",
  "runAt": "ISO8601 (tz-aware)",
  "sessionId": "uuid | null",
  "mode": "credits | byok",
  "assistantMode": "chat | code | null",
  "model": "string | null",
  "generationMode": "general | research | reasoning | study_learn | null"
}
```

- `sessionId: null` явно сбрасывает привязку (новая сессия при запуске).
- Если после патча `sessionId` непустой — `mode` из тела **игнорируется**, пишется `mode` сессии (как POST).
- Те же валидации lead/лимитов/`session_not_found`/`unsupported_*`/`run_at_timezone_required`, что у POST (применённые к новому значению).
- Пустое тело (нет полей в `model_fields_set`) → `422 empty_patch`.

### Response `200`

Обновлённый `ScheduledChat`.

| Условие | HTTP | `error.code` |
|---|---|---|
| статус ≠ `scheduled` | 409 | `not_patchable` |
| нет задачи | 404 | `scheduled_chat_not_found` |

---

## `DELETE /v1/scheduled-chats/{id}`

| Текущий статус | Поведение |
|---|---|
| `scheduled` | cancel → `status=cancelled`, `finishedAt=now()`; ответ `200` |
| `running` | `409 not_cancellable` |
| `completed` \| `failed` \| `cancelled` | физическое удаление строки из списка; `200` |

### Response `200`

```json
{ "deleted": true, "status": "cancelled | null" }
```

- После cancel: `"status": "cancelled"`.
- После hard-delete терминальной: `"status": null`.

---

## Исходящий push (APNs) — не HTTP

Клиент получает notification при переходе в `completed` или `failed` (один раз, `push_sent_at`), в т.ч. после recovery `worker_interrupted`:

```json
{
  "aps": {
    "alert": {
      "title": "Chat ready",
      "body": "Your scheduled chat has finished"
    },
    "sound": "default"
  },
  "type": "scheduled_chat_ready",
  "scheduledChatId": "<uuid>",
  "sessionId": "<uuid|null>",
  "messageStepId": "<uuid|null>",
  "status": "completed | failed",
  "errorCode": "string | null"
}
```

- **`sessionId`** = `resultSessionId ?? sessionId(planned) ?? null`.
- Deep-link: открыть чат по `sessionId`, если не `null`; иначе fallback — UI задачи / список по `scheduledChatId`.
- `notificationsEnabled=false` / нет токена / нет `APNS_*` → skip (задача всё равно в терминальном статусе).
- Ошибка APNs не откатывает `completed`/`failed`.
- **Не** вызывать `notify_media_ready`.

---

## Маппинг исхода оркестратора (сводка)

Полная таблица — [03-architecture.md](03-architecture.md). Кратко для клиента `GET` / push:

| Исход `ChatResponse` | `status` задачи | типичный `errorCode` |
|---|---|---|
| `assistant_message` | `completed` | — |
| `blocked` (policy/wallet) | `failed` | `blockReason` |
| `blocked` + `max_tokens` | `failed` | `max_tokens` |
| `tool_call` | `failed` | `tool_loop_unsupported` |
| stuck TTL / crash воркера | `failed` | `worker_interrupted` |
| сессия исчезла к run | `failed` | `session_not_found` |

---

## Коды ошибок (сводка модуля)

| `error.code` | Когда |
|---|---|
| `prompt_required` | пустой prompt |
| `prompt_too_long` | > max chars |
| `run_at_timezone_required` | `runAt` naive (без offset/Z) |
| `run_at_not_in_future` | `runAt` ≤ now |
| `run_at_too_soon` | < min lead |
| `run_at_too_far` | > max lead |
| `unsupported_mode` | mode не credits/byok |
| `unsupported_assistant_mode` | assistantMode не chat/code |
| `unsupported_generation_mode` | неизвестный generationMode |
| `unsupported_model` | model вне allowlist |
| `session_not_found` | sessionId чужой/нет (HTTP create/patch **или** worker fail) |
| `active_limit_exceeded` | слишком много активных |
| `scheduled_chat_not_found` | GET/PATCH/DELETE чужой id |
| `not_patchable` | PATCH не из scheduled |
| `not_cancellable` | DELETE cancel при running |
| `empty_patch` | PATCH без полей |
| `worker_interrupted` | stuck `running` сверх TTL (только в строке/push, не HTTP CRUD) |
| `tool_loop_unsupported` | `ChatResponse.status=tool_call` на отложенном ходе |
| `max_tokens` | `blocked` + `blockReason=max_tokens` |

При исполнении воркером в строку также пишутся доменные коды оркестратора/policy (напр. `credits_empty`, `byok_invalid`, `upstream_error`) — без нового HTTP; клиент видит их в `GET` и в push `errorCode`.
