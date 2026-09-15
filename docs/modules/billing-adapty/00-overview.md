# billing-adapty / 00 — Overview

## Назначение
Приём серверного вебхука платформы подписок Adapty и приведение состояния биллинга в соответствие событию: обновление `subscriptions` + идемпотентный грант кредитов по тиру продукта. Это **основной путь биллинга по подпискам** ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)) и второй канал начисления пакетов токенов ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E).

## In scope
- Эндпоинт `POST /v1/billing/adapty/webhook`.
- Статическая bearer-авторизация (constant-time), изолированный per-instance секрет.
- Дефенсивный приём сырого тела + ручной парсинг (без Pydantic-валидации тела).
- События (реальный набор Adapty, [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)): GRANTING (`trial_started`/`subscription_started`/`subscription_renewed`/`access_level_updated`@premium), EXPIRING (`subscription_expired`/`subscription_cancelled`/`access_level_updated`@is_active=false), NOOP (`subscription_renewal_cancelled`/`trial_renewal_cancelled` — доступ не отзывается).
- Идемпотентность ([ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md), [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A): дедуп события (`adapty_webhook_events.event_id`=`profile_event_id`) + грант **один на период по любому каналу** (ledger `sub-grant:{transaction_id}`, общий со StoreKit `sync`; фолбэк без `transaction_id` — `adapty-txn:`).
- Резолв пользователя по `customer_user_id`, затем по Adapty `profile_id` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §B).
- Пакеты токенов: `non_subscription_purchase` → грант по `one_time_credits` под `token-purchase:{transaction_id}` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E).
- Тир `vendor_product_id → tokens` (config-карта + fallback).
- Audit `adapty_subscription`.

## Out of scope (этой итерации)
- **Возвраты** (`subscription_refunded`, `non_subscription_purchase_refunded` и т. п.): `ignored` с эхом типа, кредиты не отзываются — [TD-054](../../100-known-tech-debt.md).
- **Отметка незаведённого продукта в CRM** — [TD-055](../../100-known-tech-debt.md) (в этой волне только лог и аудит, [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §D).
- **Ретирование `/v1/subscription/sync`** (StoreKit JWS). Эндпоинт остаётся рабочим; источник истины по подпискам = Adapty. Отложено — [Q-029-2](../../99-open-questions.md), [TD-021](../../100-known-tech-debt.md).
- Webhook на нашей стороне → Adapty (исходящие вызовы Adapty API). Не требуется.

## Ключевой инвариант (анти-double-grant, [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A)
Одна оплата Apple → **ровно одно** начисление по любому каналу в пределах одного `userId`. Прежняя мера «клиент использует ОДИН путь подписок» заменена защитой кодом: `sync` и вебхук пишут грант периода под одним ключом `sub-grant:{T}`, пакет токенов — под одним `token-purchase:{T}`; занятый ключ = «уже начислено». Разные `userId` у двух каналов по одной транзакции этим не закрываются ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A5). См. [05-security.md](../../05-security.md), [01-context.md](01-context.md).
