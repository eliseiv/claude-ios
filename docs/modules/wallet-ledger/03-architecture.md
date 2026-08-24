# Wallet / Ledger — Architecture

## consume (атомарно, ADR-005)
```sql
BEGIN;
INSERT INTO ledger_transactions (id, user_id, type, amount, meta, idempotency_key)
VALUES (gen_random_uuid(), :uid, 'debit', :amount, :meta, :idempotency_key)
ON CONFLICT (user_id, idempotency_key) DO NOTHING
RETURNING id;
-- :idempotency_key = значение поля requestId запроса /wallet/consume.
-- Для chat-debit Orchestrator передаёт туда messageStepId (ADR-005/ADR-006), НЕ gateway correlation requestId.
-- 0 строк -> идемпотентный повтор: SELECT существующую tx + текущий balance, COMMIT, вернуть их
-- иначе:
UPDATE wallets SET balance = balance - :amount, updated_at = now()
WHERE user_id = :uid AND balance >= :amount;
-- 0 строк -> ROLLBACK -> insufficient_credits (409)
COMMIT;
-- audit billing_debit
```
- При идемпотентном повторе сверяется, что `amount`/`meta` совпадают; иначе `409` (другой payload на тот же ключ).

## grant
Аналогично, `type=credit`, `balance + amount`, идемпотентность по ключу.

**Возвраты за генерацию медиа идут одним ключом на задачу** — `media-refund:{jobId}` ([media-generation/04-data-model.md](../media-generation/04-data-model.md)). У задачи возможны две причины возврата — провал у провайдера ([ADR-060](../../adr/ADR-060-media-generation-fal.md)) и **блокировка результата модерацией** ([ADR-086 §5](../../adr/ADR-086-ugc-moderation.md)), — но обе терминальны и делят один ключ, поэтому сложиться в два начисления не могут. Отдельного namespace под модерацию не вводится намеренно.

**Модерация входа кредитов не касается:** она отрабатывает **до** `consume` ([ADR-086 §4](../../adr/ADR-086-ugc-moderation.md)), поэтому отказ `422 content_policy_violation` не порождает ни списания, ни возврата — в ledger такого запроса не существует.

## Конкурентность
- Несколько реплик API: корректность гарантируется БД (unique index + условный UPDATE), без app-level локов.
- Изоляция: `READ COMMITTED` достаточно за счёт условия `balance >= amount` на UPDATE.

## Двойная защита баланса
1. `WHERE balance >= :amount` в UPDATE.
2. DB CHECK `balance >= 0`.

## Auto-provisioning
- Если у пользователя ещё нет `wallets`-строки — создаётся с `balance=0` при первом обращении (idempotent upsert).
