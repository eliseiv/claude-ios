# billing-cloudpayments / 00 — Overview

## Назначение
Приём серверного вебхука платёжного агрегатора **broadapps** (`pay.broadapps.dev`), который фронтит **YooKassa** и по факту успешной оплаты шлёт колбэк в **формате CloudPayments**. По событию: активировать/продлить подписку **или** начислить token-пакет, идемпотентно начислить кредиты. Это **отдельный RU-путь биллинга** ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)), независимый от Adapty ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)) и StoreKit.

## In scope
- Эндпоинт `POST /v1/billing/cloudpayments/webhook`.
- Статическая bearer-авторизация `CLOUDPAYMENTS_WEBHOOK_TOKEN` (constant-time), изолированный per-instance секрет.
- Дефенсивный приём сырого тела + ручной парсинг (без Pydantic-валидации тела). `Data` — JSON-**строка**, парсится отдельно.
- Гейт `Status=="Completed"` (ci) И `OperationType=="Payment"` (ci).
- Классификация продукта `subscription | tokens | unknown` (§[03-architecture](03-architecture.md)).
- Подписка: upsert `subscriptions` (`active`/`plan`/`expires_at`) + грант per-tier (`CLOUDPAYMENTS_PRODUCT_TOKENS` + fallback `CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT`).
- Token-пакет: разовый грант `N` строго из `TOKEN_PRODUCTS` ([ADR-015](../../adr/ADR-015-consumable-token-iap.md)).
- Идемпотентность: дедуп события (`cloudpayments_webhook_events.transaction_id`) + грант один на платёж (ledger `cp-txn:{TransactionId}`).
- Санитизация PII (карт-данные не логируются/не персистятся) + audit `cloudpayments_payment`.
- Ответ CloudPayments-стандарт `{"code":0}` на всё принятое (допущение, [Q-050-1](../../99-open-questions.md)).

## Out of scope (этой итерации)
- **Рефанды / возвраты.** Агрегатор их не шлёт; обработка не требуется.
- **Adapty-webhook, StoreKit `/v1/subscription/sync`, `/v1/tokens/purchase`** — остаются как есть, не трогаются.
- **Reject невалидного `AccountId` кодом CloudPayments (11).** Отложено ([Q-050-2](../../99-open-questions.md)) — формат кодов не подтверждён.
- **Календарно-точный `expires_at`** (relativedelta). На MVP — timedelta-приближение ([Q-050-3](../../99-open-questions.md)).
- **Создание пользователей** из тела вебхука.

## Ключевой инвариант (анти-double-grant)
Для одного `userId` на одном инстансе — один активный путь платежей. RU-путь (`cp-txn:*`) и Apple-пути (`sub-grant:*`/`adapty-txn:*`) используют **разные** ledger-namespace'ы и **не** защищают между собой. Смешение путей = риск двойного начисления (митигация контрактная/операционная, как [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)). На практике RU-инстанс avelyra ↔ broadapps.
