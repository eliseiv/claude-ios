# Wallet / Ledger — Testing

## Unit
- Расчёт нового баланса; отказ при amount > balance.

## Integration (реальный PostgreSQL, AC-3)
- Конкурентные `consume` с одним idempotency key (полем `requestId`; для chat-debit — один `messageStepId`) параллельно → ровно одно списание, остальные идемпотентно возвращают тот же txId.
- Один и тот же idempotency key (`requestId`/`messageStepId`), разный `amount` → 409.
- Re-entry message-шага: `/chat/run` → несколько `/chat/tool-result` с одним `messageStepId` → ровно один debit на финальном assistant_message.
- `consume` при balance < amount → 409 insufficient, баланс не изменён, не отрицателен.
- DB CHECK: попытка отрицательного баланса невозможна.
- `grant` идемпотентен.
- `grant` на занятый ключ с другой суммой → `ConflictError` (правило модуля не изменено [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md)); реакция вызывающих на общих ключах — [billing-adapty/09-testing.md](../billing-adapty/09-testing.md#одна-оплата--одно-начисление-adr-106).
- `has_idempotency_key` → `true` для строки под ключом, `false` для отсутствующего ключа и для того же ключа у другого `userId`.
- `GET /v1/wallet` отдаёт корректные lastTransactions в порядке убывания.
- audit billing_debit создаётся на каждое успешное списание (AC-7).
