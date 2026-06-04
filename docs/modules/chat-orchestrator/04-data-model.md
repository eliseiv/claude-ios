# Chat Orchestrator — Data Model

Владеет таблицами: `chat_sessions`, `chat_steps`, `tool_calls`. Полные DDL — в [03-data-model.md](../../03-data-model.md).

## chat_sessions
- `mode` фиксируется при создании, неизменяем на протяжении сессии.
- `updated_at` обновляется на каждом шаге (используется для soft TTL, [Q-001-1](../../99-open-questions.md)).

## chat_steps
- `payload` — content blocks (assistant text / tool_use / tool_result).
- **Вложения ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)):** для user-turn с `attachments[]` `payload["content"]` хранит текстовый блок сообщения **+ лёгкие текстовые плейсхолдеры вложений** (`[attachment: <mediaType> "<filename>", <size> — ...]`). **Сырой base64 вложений в `payload` НЕ хранится** (инвариант): контроль раздувания БД и токенов реплея. Полные image/document/text-блоки собираются in-memory только для первого вызова Anthropic message-шага и не персистятся.
- `usage` — `{inputTokens, outputTokens, model, cacheReadTokens, cacheWriteTokens}`. Без секретов.
- `message_step_id` — billing message-step id шага: генерируется в `/chat/run`, един на весь пользовательский message-шаг (все tool-раунды и re-entry). Передаётся в `Wallet.consume` как idempotency key debit ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Не путать с gateway `requestId`.
- Используется для реконструкции контекста и идемпотентного возврата следующего шага.

## tool_calls
- `id` = `toolCallId` контракта (доменный UUID, **публичный** для iOS).
- `provider_tool_use_id` — raw `tool_use.id` от Anthropic (`toolu_...`, **не** UUID), **внутренний**. Записывается при разборе `tool_use` в `/chat/run`. Используется как `tool_result.tool_use_id` при continuation, чтобы пара `tool_use`/`tool_result` в истории Anthropic совпадала по id ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md)). Тип `TEXT NOT NULL`.
- Принадлежность: `session_id`.
- `message_step_id` — тот же billing message-step id, что у шага, инициировавшего tool-call. Позволяет `/chat/tool-result` восстановить `messageStepId` для финального debit, не генерируя новый.
- `status`: `pending → completed | errored` (атомарный переход, ADR-005).
- `result` — сохранённый tool-result клиента (для идемпотентности повторной отправки).

## Инварианты
- Запись в `chat_steps`/`tool_calls` только этим модулем.
- `args`/`result`/`payload`/`usage` без API-ключей и секретов.
- `payload` user-turn **не содержит сырой base64 вложений** — только текстовые плейсхолдеры ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)).
- **Tool-id двойственность ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md)):** доменный `id` (UUID) ↔ `provider_tool_use_id` (`toolu_...`) связаны 1:1. Наружу — только доменный UUID; в Anthropic history — только `provider_tool_use_id`. Доменный id **никогда** не используется как `tool_use.id`/`tool_result.tool_use_id` в Anthropic-протоколе.
