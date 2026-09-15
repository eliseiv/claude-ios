# Subscription — Data Model

Владеет: `subscriptions`. Полный DDL — [03-data-model.md](../../03-data-model.md).

## subscriptions
- PK = `user_id` (одна активная запись на пользователя).
- `status` ∈ {active, expired, none}.
- `expires_at` — конец текущего периода (nullable для none).
- `ix_subscriptions_expires_at` — для фоновых проверок истечения.

## Связь с ledger
- Идемпотентность grant — `idempotency_key` = `sub-grant:<transactionId>` в `ledger_transactions`; ключ **общий** с вебхуком Adapty при настоящем `transaction_id`, исторический ключ вебхука `adapty-txn:<transactionId>` проверяется до гранта ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A).

## Инварианты
- Пишут в `subscriptions` (по коду): `sync` (этот модуль), вебхук Adapty (`AdaptyWebhookService`), вебхук CloudPayments (`CloudPaymentsWebhookService`), ручная выдача плана (`POST /v1/admin/subscription/grant`, CRM «Установить план»). Прежнее утверждение «только этот модуль» — неверно (уточнение факта).
- Статус нормализуется при каждом sync, кроме устаревшей транзакции: её срок раньше срока активной строки — строка не меняется ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §C; тот же предикат у вебхука Adapty).
