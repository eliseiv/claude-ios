# billing-adapty / 01 — Context

## Соседи и зависимости

| Зависимость | Что используется | Источник |
|---|---|---|
| Auth-образец | constant-time bearer (`hmac.compare_digest`), `auto_error=False` security scheme | `src/app/api_gateway/auth.py:99-134`, `src/app/api_gateway/openapi_security.py` |
| Wallet | `WalletService.grant(*, user_id, amount, idempotency_key, meta, reason) -> GrantResult` — идемпотентный кредит-грант | `src/app/wallet/service.py` ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)) |
| Subscription | upsert строки `subscriptions` (status/plan/expires_at); предикат устаревания — общий с `sync` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §C) | `src/app/subscription/service.py` (`SubscriptionService.sync`) |
| Token purchase | ключ покупки `token-purchase:{transactionId}` (`_IDEMPOTENCY_PREFIX`) и резолвер суммы `one_time_credits` — общие с `non_subscription_purchase` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E) | `src/app/token_purchase/service.py`, `src/app/instance_config/products.py` |
| Config | образец JSON-парсинга env-карты `token_products()` | `Settings.token_products()`, `src/app/config.py` |
| Audit | `AuditService.record`, `assert_no_secrets` | `src/app/audit/service.py`, `src/app/observability/redaction.py` |
| Policy | читает `subscriptions.status` (active/expired) | [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) |
| Router registration | `app.include_router(...)`, глобального auth-middleware нет | `src/app/main.py` |

## Кто вызывает
- **Adapty (внешний сервис)** — серверный HTTP POST. Не наш iOS-клиент. Аутентификация — статический bearer-секрет, заданный оператором в Adapty UI.

## Соотношение с существующими путями биллинга
- `POST /v1/subscription/sync` (модуль [subscription](../subscription/README.md)) — StoreKit JWS, **остаётся**. Грант периода — под общим ключом `sub-grant:{transactionId}` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A).
- `POST /v1/tokens/purchase` (модуль [token-purchase](../token-purchase/README.md)) — consumable IAP, **остаётся**; тот же пакет может начислить вебхук `non_subscription_purchase` под тем же ключом `token-purchase:{transactionId}` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E).

> **Инвариант:** одна оплата — одно начисление по любому каналу, общими ключами (см. [00-overview.md](00-overview.md), [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A). Требование «один путь подписок на клиенте» снято.

## Данные
- Использует существующие таблицы `users`, `auth_devices`, `legacy_user_ids` (резолв `customer_user_id`/`profile_id` через `resolve_user`), `subscriptions`, `wallets`, `ledger_transactions`.
- Вводит новую таблицу `adapty_webhook_events` (см. [04-data-model.md](04-data-model.md), миграция `0008`).
