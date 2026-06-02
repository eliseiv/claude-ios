# Admin — API Contracts

Все admin-эндпоинты под префиксом `/v1/admin/*`. Авторизация — заголовок `X-Admin-Token` (изолированный
admin-секрет, [ADR-009](../../adr/ADR-009-admin-token-auth.md)), зависимость `require_admin`. **Пользовательский
JWT не авторизует admin-действия.** Отсутствие/несовпадение токена → `401`. Отдельный rate limit (дефолт 10 req/min
per source IP, конфигурируемо), `extra='forbid'`, тело ≤ 8 KB.

## POST /v1/admin/wallet/grant
Начисление кредитов пользователю (саппорт/компенсация).

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Request
```json
{
  "userId": "uuid",
  "amount": 100,
  "idempotencyKey": "string",
  "reason": "string"
}
```
- `userId` — UUID существующего пользователя (см. Правила §Несуществующий userId).
- `amount` — целое **> 0** (BIGINT, целые кредиты, без дробей). `amount <= 0` → `422`.
- `idempotencyKey` — непустая строка, `max_length` 128. Ключ идемпотентности начисления (передаётся в `WalletService.grant(idempotency_key=...)`).
- `reason` — **обязателен**, непустая строка, `max_length` 512. Пишется в audit `admin_grant` и `ledger_transactions.meta`.

### Response (200)
```json
{
  "newBalance": 1100,
  "ledgerTxId": "uuid",
  "idempotentReplay": false
}
```
- `newBalance` — баланс после начисления.
- `ledgerTxId` — id `ledger_transactions` (`type=credit`).
- `idempotentReplay` — `true`, если ключ уже был использован с тем же payload (повторного начисления не было).

### Правила
- Переиспользует `WalletService.grant(user_id, amount, idempotency_key, meta, reason)` (`src/app/wallet/service.py:174`)
  — атомарно, идемпотентно по `(user_id, idempotency_key)`, пишет `ledger_transactions(type=credit)` + audit `billing_credit`.
- **Дополнительно** пишется audit-событие `admin_grant` (actor=admin, `userId`, `amount`, `reason`, `idempotencyKey`,
  `ledgerTxId`) — отдельно от `billing_credit`, фиксирует именно admin-инициацию. **Секрет `X-Admin-Token` в audit не пишется.**
- Идемпотентность: тот же `idempotencyKey` + тот же payload → тот же `ledgerTxId`, `idempotentReplay=true`, без повторного начисления.
- Тот же `idempotencyKey`, **другой** `amount` → `409` (конфликт, как в `WalletService.grant`), без начисления.
- **Несуществующий userId → `404 {error.code:"user_not_found"}`** (admin-grant **не создаёт** пользователей — см. 03-architecture; обоснование ниже).
- `reason` отсутствует/пустой → `422`.

## GET /v1/admin/wallet/{userId}
Read-only просмотр кошелька для саппорта.

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Response (200)
```json
{
  "userId": "uuid",
  "balance": 1100,
  "lastTransactions": [
    { "id": "uuid", "type": "credit|debit", "amount": 100, "createdAt": "ISO8601", "meta": {} }
  ]
}
```
- Переиспользует `WalletService.get_wallet_view(user_id, last_n)` (дефолт `last_n=20`, по `created_at DESC`).
- `meta` — без секретов (usage/model/reason).

### Правила
- Несуществующий `userId` → `404 {error.code:"user_not_found"}` (read-only не создаёт пользователя).
- Только чтение; не мутирует состояние и не пишет мутирующий audit (логируется на уровне tool/request lifecycle).

## Обоснование «404 на несуществующем userId» (не admin-provisioning)
Admin-grant **не создаёт** пользователей. Причины:
- Источник истины идентичности — доверенный JWT issuer ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md));
  создание `users` из admin-API ввело бы второй, неаутентифицированный путь рождения идентичности и риск
  начисления на «фантомный» (опечатанный) `userId`.
- `404` делает опечатку в `userId` видимой оператору сразу, вместо молчаливого создания мусорного аккаунта с балансом.
- Реальные пользователи создаются лениво при первом аутентифицированном запросе (ADR-007); к моменту легитимного
  admin-grant пользователь, как правило, уже существует. Если нужно начислить «наперёд» — это отдельный продуктовый
  вопрос, не решается тихим созданием строки. См. [Q-009-2](../../99-open-questions.md) (не блокер; дефолт — `404`).
