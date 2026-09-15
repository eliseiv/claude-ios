# Token Purchase — Context

## Зависимости
- **API Gateway** — auth, provisioning, роут `/v1/tokens/*`.
- **subscription** (verifier) — переиспользует StoreKit-верификатор (реальная JWS / App Store Server API, fail-closed; `STOREKIT_TEST_MODE` для e2e/CI, [TD-007](../../100-known-tech-debt.md)). Verifier выделен как общий компонент, не дублируется.
- **wallet-ledger** — `Wallet.grant(credits, idempotency_key="token-purchase:{transactionId}", type=credit, meta={source:token_purchase, productId, transactionId})`. Единственный, кто пишет в ledger (инвариант сохранён).
- **billing-adapty** — второй производитель ключа `token-purchase:{transactionId}` (событие `non_subscription_purchase`, [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E); сумма — тот же резолвер `one_time_credits`.

## Разграничение с subscription ([ADR-015](../../adr/ADR-015-consumable-token-iap.md))
- `subscription/sync` → grant фикс. пакета на период (idempotency = transactionId периода), ADR-006 — без изменений.
- `tokens/purchase` → grant под `token-purchase:{transactionId}`. Префиксы ключей (`sub-grant:` / `token-purchase:`) разные; `meta.source` различает.

## Границы
- Token-purchase — тонкая обёртка: verify → map → grant. Не вызывает Anthropic, не меняет policy-логику, не трогает subscription-статус.
