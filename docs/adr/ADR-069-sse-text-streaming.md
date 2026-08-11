# ADR-069 — SSE text streaming для chat v2 (`/v1/chat/v2/run/stream`)

- **Статус:** Accepted
- **Дата:** 2026-08-11
- **Связано:** [TD-018](../100-known-tech-debt.md), [ADR-025](ADR-025-parallel-tool-calls-and-max-tokens-truncation.md), [ADR-033](ADR-033-llm-provider-abstraction.md), [ADR-064](ADR-064-study-learn-quiz-generation-mode.md)

## Контекст

`POST /v1/chat/v2/run` отвечает одним JSON после полного non-streaming вызова провайдера. На длинных ответах iOS показывает «думает» до конца — старый бэк отдавал `partial_result` с растущим текстом. В спеке не было SSE/WebSocket ([TD-018](../100-known-tech-debt.md)).

## Решение

### 1. Новый эндпоинт, старый JSON не ломаем

`POST /v1/chat/v2/run/stream` — тот же body/auth/rate-limit, что у `/v1/chat/v2/run`, ответ `Content-Type: text/event-stream`.

События SSE (`event` + JSON `data`):

| event | data |
|-------|------|
| `delta` | `{ "text": "<incremental>" }` |
| `done` | полный `ChatResponse` (как JSON `/v2/run`) |
| `error` | `{ "code", "message" }` при сбое после старта стрима (HTTP уже 200) |

Legacy `/v1/chat/run` и `/v1/chat/v2/tool-result` без stream в этой итерации.

### 2. Только текст

Стримятся только текстовые дельты ассистента. `toolCalls` / `serverTools` / `mediaJobs` / `quiz` приходят только в `done`.

Дельты эмитятся **по мере прихода** от провайдера (прогрессивный UX). Режим `study_learn` — **без** `delta` (анти-спойлер квиза); только `done`.

### 3. Клиенты LLM

Аддитивный `LLMClient.stream_message(...)` → `AsyncIterator[StreamEvent]` (`text_delta` | `completed(LLMResult)`). Реализации: Anthropic `messages.stream`, OpenAI Chat Completions `stream=True`, OpenAI Responses `stream=True`. Финальный `LLMResult` совпадает по смыслу с `create_message`.

### 4. Биллинг / policy

Без изменений: policy до генерации; debit на финальном `assistant_message` как в non-stream. Policy-`blocked` → один `done` без дельт.

### 5. TD-018

UX-часть (прогрессивный текст) закрыта этим ADR. Стрим partial tool_use / «дописать с места» после `max_tokens` остаётся долгом.

## Последствия

- iOS: новый URL, парсинг SSE; UI растёт по `delta`, истина — `done.assistantMessage`.
- Traefik: не буферизовать ответ стрим-роута (`X-Accel-Buffering: no`).
- Без миграции БД.
