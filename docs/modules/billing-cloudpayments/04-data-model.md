# billing-cloudpayments / 04 — Data Model

## Новая таблица: `cloudpayments_webhook_events`

Журнал применённых платёжных событий broadapps (CloudPayments-формат). Единая точка дедупликации доставки (UNIQUE `transaction_id`) + аудиторский след **санитизированного** payload (без карт-данных). Миграция **`0014`** (`down_revision="0013"`, single head, expand-only).

| Колонка | Тип | Ограничения | Назначение |
|---|---|---|---|
| `transaction_id` | `text` | **PRIMARY KEY** (= UNIQUE) | `TransactionId` (top-level) — точка дедупа доставки события |
| `user_id` | `uuid` | `NOT NULL`, FK `users(id) ON DELETE CASCADE` | целевой пользователь (`AccountId`, нормализован к lower → UUID) |
| `product_id` | `text` | `NOT NULL` | `Data.product_id` |
| `kind` | `text` | `NOT NULL` | классификация: `subscription` \| `tokens` |
| `payload` | `jsonb` | `NOT NULL` | **санитизированная** проекция события (без PII карт/токена) |
| `processed_at` | `timestamptz` | `NOT NULL DEFAULT now()` | момент обработки |

### DDL (ориентир для миграции 0014)
```sql
CREATE TABLE cloudpayments_webhook_events (
    transaction_id text        PRIMARY KEY,
    user_id        uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id     text        NOT NULL,
    kind           text        NOT NULL,
    payload        jsonb       NOT NULL,
    processed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_cloudpayments_webhook_events_user_id ON cloudpayments_webhook_events (user_id);
```

### Санитизированный `payload` (allowlist — что МОЖНО хранить)
`transactionId`, `productId`, `kind`, `status`, `operationType`, `amount`, `currency`, `testMode`, `billingIntervalUnit`, `billingIntervalCount`, `billingPhase`, `subscriptionId`.

**ЗАПРЕЩЕНО** в `payload` (PII/секреты): `CardFirstSix`, `CardLastFour`, `Issuer`, `CardType`, `Authorization`/bearer, `Data`-строка целиком, любые прочие карт-поля. Отличие от `adapty_webhook_events` (там хранится полный parsed-объект — карт-данных нет; здесь — только явный allowlist).

### Замечания
- `transaction_id` как PK даёт UNIQUE-гарантию и `ON CONFLICT (transaction_id) DO NOTHING` для дедупа (см. [03-architecture.md](03-architecture.md)).
- Событие пишется в журнал **только на `applied`** (валидный, начисляемый платёж); `ignored`/`duplicate` строк не создают.
- Index по `user_id` — для будущих выборок «платежи пользователя» (диагностика). В горячем пути запросов по нему нет.

## Затронутые существующие таблицы (без изменения схемы)
- `subscriptions` — upsert по `user_id` (`ON CONFLICT (user_id) DO UPDATE`, образец [ADR-048](../../adr/ADR-048-admin-subscription-grant.md)) для класса `subscription` (`status='active'`, `plan=product_id`, `expires_at`). Для `tokens` — **не трогается**. Схема: `src/app/models/tables.py`, enum `subscription_status ∈ none|active|expired`.
- `ledger_transactions` — грант кредитов идемпотентно по `(user_id, idempotency_key="cp-txn:{transaction_id}")` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)). Один грант на платёж. Namespace `cp-txn:*` изолирован. Схема не меняется.
- `wallets` — баланс инкрементируется внутри `WalletService.grant`.
- `users` — lookup по id (нормализованный `AccountId`); **не создаётся** вебхуком.

## ORM
Добавить модель `CloudPaymentsWebhookEvent` в `src/app/models/tables.py`. Без новых enum-типов.

## Миграция 0014 (инварианты)
- `down_revision = "0013"` (после `byok_provider`); **single head** — проверить `alembic heads` = один.
- **Expand-only** (только CREATE TABLE + CREATE INDEX), без backfill, без изменения существующих таблиц.
- `upgrade` создаёт таблицу+индекс; `downgrade` — `DROP TABLE cloudpayments_webhook_events`.
