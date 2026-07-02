# billing-cloudpayments / 09 — Testing (ориентиры для qa)

Тесты по [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md), герметичные (без сети/реального broadapps). Стек/команды — [docs/02-tech-stack.md](../../02-tech-stack.md), [docs/06-testing-strategy.md](../../06-testing-strategy.md). Плейсхолдер-секрет `CLOUDPAYMENTS_WEBHOOK_TOKEN` в тестах.

## Авторизация
- Нет заголовка / неверный bearer → `401`.
- `CLOUDPAYMENTS_WEBHOOK_TOKEN==""` → `500` (мис-конфигурация) на любой запрос.
- Верный bearer → доходит до парсинга.

## HTTP-контракт (всё `200 {"code":0}` кроме 401/500)
- Пустое тело → `200 {"code":0}` (лог `ignored/empty_body`, DEBUG).
- Не-JSON / JSON-не-объект → `200 {"code":0}`.
- `Status!="Completed"` или `OperationType!="Payment"` → `200 {"code":0}` (`not_a_completed_payment`).
- Нет `TransactionId` → `missing_transaction_id`.
- `Data` не парсится / нет → `invalid_data`.
- Нет `product_id` → `missing_product_id`.
- Нет/не-UUID `AccountId` и `Data.user_id` → `invalid_account_id`.

## Парсинг
- `userId` ← `AccountId` (верх), fallback `Data.user_id`; **верхний регистр → нормализация lower** → находит `users`.
- `Data` как JSON-строка → извлечены `product_id`/`billing_interval_unit`/`billing_interval_count`(str→int).
- `billing_interval_count="1"` → int 1; невалид → 1.
- Карт-данные (`CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`) **не** попадают в `ParsedPayment`.

## Классификация продукта
- `product_id ∈ TOKEN_PRODUCTS` → `tokens` (приоритет карты).
- `billing_interval_unit="year"` (не в TOKEN_PRODUCTS) → `subscription`.
- `"100_tokens_pack"` не в TOKEN_PRODUCTS → `tokens` по паттерну, но `token_products().get` пусто → `ignored/unknown_product` (WARNING), **без** записи события.
- `"yearly_49.99_nottrial"` без `billing_interval_unit` → `subscription` по имени.
- мусорный `product_id` без сигналов → `unknown` → `ignored/unknown_product`.

## Маппинг / начисление
- **Реальный payload годовой подписки** (пример из ADR-050) → `subscriptions.status=active`, `plan="yearly_49.99_nottrial"`, `expires_at ≈ now+365д`; **один** ledger-грант с ключом `cp-txn:{TransactionId}`; сумма = `CLOUDPAYMENTS_PRODUCT_TOKENS[product_id]` или fallback `CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT`.
- token-пакет → разовый грант `N=TOKEN_PRODUCTS[product_id]`, `subscriptions` **не** изменена.
- Пользователь не найден → `ignored/user_not_found` (WARNING), нет вставки в `users`/журнал, баланс не изменён.

## Идемпотентность
- Повтор того же `TransactionId` → `duplicate`, баланс/подписка не изменились (двойная граница: UNIQUE журнала + ledger-ключ).
- Продление: новый `TransactionId`, тот же `subscription_id` → **новый** грант + `expires_at` сдвинут (не схлопывается).
- Гонка двух одинаковых `TransactionId` → ровно один `applied`, второй `duplicate` (ON CONFLICT).

## Наблюдаемость / PII
- На каждый исход — ровно одна запись `"cloudpayments_webhook_outcome"`; уровни по таблице [08-observability.md](08-observability.md).
- В логах и в `cloudpayments_webhook_events.payload` **нет** карт-данных, bearer, сырого `Data`. `payload` = только allowlist ([04-data-model.md](04-data-model.md)).
- Audit `cloudpayments_payment` пишется только на `applied`; `assert_no_secrets` не падает.

## Изоляция (регресс существующего)
- Adapty-webhook, `/v1/subscription/sync`, `/v1/tokens/purchase`, BYOK — поведение не изменилось.
- Ledger-namespace `cp-txn:*` не пересекается с `adapty-txn:*`/`sub-grant:*`/`admin-sub-grant:*`.
- Миграция `0014`: `alembic heads` = один; `upgrade`/`downgrade` чистые.

## Swagger-чистота
- В OpenAPI (`/openapi.json`) у роута нет вхождений `ADR-`/`Q-`/`TD-` и внутренних имён таблиц/namespace ([R2ter](../../08-api-documentation.md)).
