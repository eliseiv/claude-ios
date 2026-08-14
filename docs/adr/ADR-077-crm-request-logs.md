# ADR-077 — История запросов CRM из `request_logs`, а не из `audit_logs`

- **Статус:** accepted
- **Дата:** 2026-08-14
- **Область:** CRM Admin API, chat, media generation, data model

## Контекст

`GET /v1/admin/users/{id}/requests` ошибочно трактовал каждую строку
`audit_logs` как запрос к backend: `event_type` становился `endpoint`, а
отсутствующие HTTP-метрики синтезировались. Поэтому в CRM отображались
`billing_debit`, `policy_decision`, `chat_step`, выдуманный `200`, пустая
длительность и отсутствующая экономика.

`audit_logs` — журнал доменных/security/billing-событий, а не журнал API
запросов. Фильтр только по `event_type='chat_step'` не исправляет источник:
он не даёт реальный endpoint, HTTP-исход, длительность и media-запросы.

## Решение

### 1. Отдельная таблица

Миграция `0023_request_logs` вводит `request_logs`:

- `id`, `user_id`;
- реальный `endpoint`, усечённый `prompt_preview`;
- внутренний `status`: `started|queued|completed|failed`;
- `status_code`;
- `started_at`, `completed_at`; `duration_sec` вычисляется при чтении;
- `tokens_spent` — экономические credits, реально списанные с пользователя;
- `provider_cost_usd` — nullable; `NULL` означает «не измерено»;
- `refunded` — отдельный факт возврата, не обнуляющий `tokens_spent`;
- корреляция `message_step_id` и `media_job_id`.

Индекс списка: `(user_id, started_at DESC)`. `media_job_id` уникален: одна
асинхронная media-задача соответствует одной строке. `message_step_id` не
уникален: один логический turn может продолжаться несколькими HTTP
`tool-result` запросами, каждый из которых является отдельной строкой.

`user_id` — soft reference на verified JWT `sub`, без FK. На первом запросе
lazy-provisioning создаёт `users` в ещё незакоммиченной request-транзакции;
независимый writer с FK ожидал бы этот INSERT, пока request ожидает writer
(deadlock). Writer недоступен до auth, а CRM перед чтением отдельно проверяет
пользователя, поэтому мягкая ссылка сохраняет доверительную границу и позволяет
логировать самый первый запрос.

### 2. Один HTTP-вызов — одна строка

Chat routes (`run`, `v2/run`, `v2/run/stream`, оба `tool-result`) создают
строку после auth/rate-limit и до вызова orchestrator. Успех завершает её
реальным HTTP-кодом. Техническая ошибка записывает `failed` и реальный
`AppError.status_code` (иначе 500).

SSE-ошибка после отправки первого frame всё равно завершает строку как
`failed`: транспортный HTTP уже равен 200, но доменный исход запроса — ошибка.

Для chat `tokens_spent` приходит из результата `Wallet.consume`:
`amount` только при новом debit и `0` при `idempotent_replay`. Временные
эвристики по `ledger.created_at` запрещены (`now()` в PostgreSQL относится к
началу транзакции). Input/output LLM tokens — другая единица и не подменяют
credits.

### 3. Media lifecycle

Успешный submit создаёт одну строку `queued/202`, связанную с
`media_job_id`, с реальным `credits_charged`. Poll и background reconciler
используют один terminal path:

- completed → `completed`, `200`, `completed_at`;
- failed → `failed`, `500`, `completed_at`, `refunded` из media job.

Terminal update идемпотентен и обновляет только незавершённую строку.

### 4. Независимые короткие транзакции

Writer использует собственную короткую `AsyncSession` на каждую операцию.
Start/error должны переживать rollback request-scoped session. Ошибка
телеметрии не меняет бизнес-ответ, но логируется как operational error.

### 5. CRM mapping

- `completed`, duration ≤30s → `ok`;
- `completed`, duration >30s → `slow`;
- `started|queued` → `slow`, HTTP 202;
- `failed` → `error`.

`provider_cost_usd` остаётся `NULL`, пока в claude-ios нет отдельной
проверенной карты provider pricing. Ноль не выдумывается.

## Миграция и совместимость

`audit_logs` не backfill-ятся: достоверно восстановить request history из них
невозможно. После выката история начинается с новых запросов; пустой список
до первого запроса разрешён CRM-контрактом. Audit продолжает писаться без
изменений.

Rollout: сначала миграция `0023`, затем приложение. Откат приложения
совместим с оставшейся неиспользуемой таблицей; downgrade удаляет её.

## Альтернативы

- Фильтровать audit events — отклонено: метрики остаются синтетическими.
- Строить историю из `chat_steps`/`media_jobs` — отклонено: нет полного
  HTTP-lifecycle и ошибок до создания доменных строк.
- Возвращать пустой список — безопасно, но не выполняет требование полной
  истории новых запросов.
