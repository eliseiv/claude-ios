# Wallet / Ledger — Overview

## Scope
- `GET /v1/wallet` — баланс + последние транзакции.
- `POST /v1/wallet/consume` — атомарное идемпотентное списание.
- Внутренний `grant(userId, amount, idempotency_key, meta, reason)` — начисление кредитов (тип `credit`). Вызывают: Subscription, billing-adapty, token-purchase, billing-cloudpayments, admin, media-generation (возвраты) ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md), [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md)).
- Единственный writer для `wallets` и `ledger_transactions`.

## Out of scope
- Решение, можно ли списывать (Policy Engine).
- Определение `amount` (Orchestrator передаёт готовое значение: `amount=1` для credits-debit по [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md); Wallet не конвертирует usage).
- Публичная покупка кредитов (out of scope bootstrap).

## Ключевые гарантии
- Атомарность + идемпотентность ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)).
- `balance >= 0` всегда (DB CHECK + условие в UPDATE).
