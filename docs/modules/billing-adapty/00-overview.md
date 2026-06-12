# billing-adapty / 00 — Overview

## Назначение
Приём серверного вебхука платформы подписок Adapty и приведение состояния биллинга в соответствие событию: обновление `subscriptions` + идемпотентный грант кредитов по тиру продукта. Это **основной путь биллинга по подпискам** ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)).

## In scope
- Эндпоинт `POST /v1/billing/adapty/webhook`.
- Статическая bearer-авторизация (constant-time), изолированный per-instance секрет.
- Дефенсивный приём сырого тела + ручной парсинг (без Pydantic-валидации тела).
- 4 типа событий: `subscription_started`, `subscription_renewed`, `subscription_cancelled`, `subscription_expired`.
- Идемпотентность через таблицу `adapty_webhook_events` (UNIQUE `event_id`) + ledger idempotency-key.
- Тир `vendor_product_id → tokens` (config-карта + fallback).
- Audit `adapty_subscription`.

## Out of scope (этой итерации)
- **Consumable-пакеты токенов через Adapty.** Остаются на `/v1/tokens/purchase` ([ADR-015](../../adr/ADR-015-consumable-token-iap.md)). Перенос — [Q-029-1](../../99-open-questions.md), [TD-020](../../100-known-tech-debt.md).
- **Ретирование `/v1/subscription/sync`** (StoreKit JWS). Эндпоинт остаётся рабочим; источник истины по подпискам = Adapty. Отложено — [Q-029-2](../../99-open-questions.md), [TD-021](../../100-known-tech-debt.md).
- Webhook на нашей стороне → Adapty (исходящие вызовы Adapty API). Не требуется.

## Ключевой инвариант (анти-double-grant)
Клиент использует **ОДИН** путь биллинга подписок. На Adapty-сборке iOS шлёт только Adapty-события и **не** вызывает `/v1/subscription/sync`. Иначе — разные idempotency-ключи → двойное начисление (см. [05-security.md](../../05-security.md), [01-context.md](01-context.md)).
