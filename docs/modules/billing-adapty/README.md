# Module: Billing — Adapty

- Статус: Спроектирован, ожидает реализации
- Ответственность: приём серверного вебхука Adapty (`POST /v1/billing/adapty/webhook`), идемпотентное обновление подписки и грант кредитов по тиру продукта. Основной путь биллинга по подпискам ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)).

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md)

## DoD
- Эндпоинт авторизуется статическим bearer-секретом (constant-time); неверный/нет токена → 401; секрет не задан → 500.
- После авторизации любое тело (пустое/не-JSON/неполное/неизвестное событие/дубликат) → `2xx` (Adapty сохраняет вебхук и не зацикливает ретраи). `5xx` только при реальном сбое.
- `subscription_started/renewed` → `subscriptions.status=active` + идемпотентный грант кредитов по тиру; `subscription_cancelled/expired` → `status=expired`, кредиты не трогаются.
- Идемпотентность: повтор того же `event_id` → `duplicate`, без двойного начисления (две UNIQUE-границы).
- Audit-событие `adapty_subscription` пишется через `assert_no_secrets`.

## Границы (см. [00-overview.md](00-overview.md))
- **НЕ** покрывает consumable-пакеты токенов (остаются на `/v1/tokens/purchase`, [ADR-015](../../adr/ADR-015-consumable-token-iap.md), [Q-029-1](../../99-open-questions.md)).
- **НЕ** ломает `/v1/subscription/sync` (StoreKit JWS) — он остаётся, но источник истины по подпискам = Adapty. Ретирование `sync` отложено ([Q-029-2](../../99-open-questions.md), [TD-021](../../100-known-tech-debt.md)).

## Changelog
- 2026-06-12: проектирование (architect), [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md). Новая таблица `adapty_webhook_events` (миграция `0008`), env `ADAPTY_WEBHOOK_SECRET` / `ADAPTY_PRODUCT_TOKENS` / `ADAPTY_SUBSCRIPTION_TOKENS_GRANT`. Заведены [Q-029-1/2/3](../../99-open-questions.md), [TD-020/021](../../100-known-tech-debt.md).
