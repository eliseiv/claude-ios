# Admin — Testing

## Unit
- `require_admin`: валидный `X-Admin-Token` → проходит; неверный/отсутствует → `401`; сравнение constant-time (по контракту,
  не таймингом). Совпадение с `ADMIN_API_SECRET_PREV` (ротация) → проходит. Пустые секреты в конфиге не матчатся.
- Pydantic-схема grant: `amount<=0` → `422`; пустой/отсутствующий `reason` → `422`; лишнее поле (`extra='forbid'`) → `422`.

## Integration (реальный PostgreSQL)
- `grant` на существующем `userId` → `ledger_transactions(type=credit)`, баланс += amount, `idempotentReplay=false`.
- Повторный `grant` тот же `idempotencyKey` + payload → `idempotentReplay=true`, баланс не меняется, тот же `ledgerTxId`.
- Тот же `idempotencyKey`, другой `amount` → `409`, без начисления.
- Несуществующий `userId` → `404 user_not_found`, строка `users` **не** создана, баланс не появился.
- `require_admin` не создаёт `users` для actor (нет provisioning): после серии admin-запросов нет «admin»-строки в `users`.
- `users.trial_used` не изменяется admin-операциями.
- audit: успешный `grant` создаёт **и** `billing_credit` (Wallet), **и** `admin_grant` (Admin). Секрет в payload отсутствует.
- `GET /v1/admin/wallet/{userId}`: корректные `balance` + `lastTransactions` (DESC); несуществующий → `404`.

## Security
- Пользовательский JWT на `/v1/admin/*` (без `X-Admin-Token`) → `401` (JWT не авторизует admin).
- `X-Admin-Token` на пользовательском роуте (`/v1/wallet`) не даёт доступа (там нужен JWT).
- `X-Admin-Token` не попадает в логи/audit (redaction).
- Rate limit `/v1/admin/*`: превышение дефолта → `429`, изолировано от пользовательских лимитов.
- Size-лимит admin-grant: тело > 8 KB → `413`.
