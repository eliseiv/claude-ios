# Module: Billing — CloudPayments (broadapps / YooKassa, RU-путь)

- Статус: Спроектирован, ожидает реализации
- Ответственность: приём серверного вебхука агрегатора **broadapps** (`pay.broadapps.dev`, фронтит YooKassa) в **формате CloudPayments** (`POST /v1/billing/cloudpayments/webhook`), идемпотентная активация подписки / начисление token-пакета и грант кредитов. **Отдельный RU-путь биллинга** ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)), независимый от Adapty и StoreKit.

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [08-observability.md](08-observability.md) · [09-testing.md](09-testing.md)

## DoD
- Эндпоинт авторизуется статическим bearer `CLOUDPAYMENTS_WEBHOOK_TOKEN` (constant-time, per-route); нет/неверный токен → `401`; секрет не задан → `500` (⇒ активен только на avelyra).
- После авторизации любое тело (пустое/не-JSON/неполное/неизвестный продукт/дубликат/неизвестный пользователь) → **`200` c `{"code":0}`** (агрегатор не ретраит). `500` только при реальном сбое БД / незаданном секрете.
- `userId` ← `AccountId` (верх) → fallback `Data.user_id`; **нормализация к lower**; не-UUID → `ignored/invalid_account_id`; не найден → `ignored/user_not_found` (без создания пользователя).
- Гейт: `Status=="Completed"` (ci) И `OperationType=="Payment"` (ci); иначе `ignored/not_a_completed_payment`.
- `Data` — JSON-**строка** (парсится отдельно); классификация продукта → subscription (upsert `active`+`plan`+`expires_at` + грант per-tier) ИЛИ tokens (разовый грант `N` из `TOKEN_PRODUCTS`) ИЛИ unknown (`ignored/unknown_product`, WARNING).
- Идемпотентность: дедуп события по `TransactionId` (UNIQUE `cloudpayments_webhook_events.transaction_id`); грант — один на платёж (ledger `cp-txn:{TransactionId}`, изолирован).
- **PII:** карт-данные (`CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`) и bearer **не логируются и не персистятся**; `payload` таблицы и лог/audit — только санитизированная проекция (allowlist).
- Audit `cloudpayments_payment` через `assert_no_secrets`.
- **Swagger-чистота** ([R2ter](../../08-api-documentation.md)): user-facing OpenAPI-строки без ADR/TD/Q и внутреннего жаргона.

## Границы (см. [00-overview.md](00-overview.md))
- **НЕ** трогает Adapty-webhook ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)/[ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)), StoreKit `/v1/subscription/sync`, `/v1/tokens/purchase`, BYOK, LLM-абстракцию, policy-engine.
- **НЕ** обрабатывает рефанды (агрегатор их не шлёт).
- **НЕ** создаёт пользователей.

## Changelog
- 2026-07-02: проектирование (architect), [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md). Новый модуль/эндпоинт `POST /v1/billing/cloudpayments/webhook`, новая таблица `cloudpayments_webhook_events` (миграция `0014`, down_revision `0013`), env `CLOUDPAYMENTS_WEBHOOK_TOKEN` / `CLOUDPAYMENTS_PRODUCT_TOKENS` / `CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT`. Заведены [Q-050-1..4](../../99-open-questions.md). Активен только на avelyra.
