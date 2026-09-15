# Subscription — API Contracts

## POST /v1/subscription/sync
### Request
```json
{
  "userId": "uuid",
  "transaction": { "...StoreKit transaction payload (signed)..." }
}
```
- `transaction` — подписанный StoreKit payload (JWS signed transaction / App Store receipt). Конкретный формат — App Store Server API.

### Response (200)
```json
{
  "isSubscribed": true,
  "expiresAt": "ISO8601 | null",
  "plan": "string | null"
}
```

### Правила
- Сервер **верифицирует** транзакцию (подпись/через App Store Server API), не доверяет клиенту.
- Идемпотентность: грант периода — под ключом `sub-grant:{transactionId}`, **общим с вебхуком Adapty** ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A). До гранта проверяются `sub-grant:{transactionId}` и исторический `adapty-txn:{transactionId}`; есть хоть один → гранта нет. Повтор `sync` той же транзакции и транзакция, уже зачисленная вебхуком Adapty, не начисляют повторно.
- Занятый ключ периода с другой суммой (суммы каналов калибруются раздельно) — **не ошибка**: `200` с текущим состоянием, без `409` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A3).
- При активной транзакции нового периода → Wallet.grant: сумма `subscription_credits(productId, storekit)` — оверлей по `productId`, иначе `SUBSCRIPTION_CREDITS_PER_PERIOD` (дефолт 1000) ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md), [ADR-099 §6](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md)). Продукт, которого нет в каталоге инстанса, начисляется фолбэком с WARNING и аудитом `subscription_product_unmapped` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §D).
- **Устаревшая транзакция** ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §C): у пользователя активная подписка со сроком позже `expiresDate` транзакции → строка `subscriptions` не меняется; ответ `200` = **текущее состояние строки** (`isSubscribed` — активна и не истекла, `expiresAt`/`plan` строки); грант — по правилу ключа выше.
- **Разовая покупка пакетов токенов** (consumable IAP) — **отдельный** endpoint `POST /v1/tokens/purchase` (модуль [token-purchase](../token-purchase/README.md), [ADR-015](../../adr/ADR-015-consumable-token-iap.md)), НЕ через `subscription/sync`. Использует общий StoreKit verifier, но отдельный путь grant (idempotency по consumable `transactionId`, `meta.source=token_purchase`). Subscription grant этим не затрагивается.
- refund/revocation и `isUpgraded=true` (транзакция заменена апгрейдом, [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §C4) → транзакция неактивна: `status=expired`, `isSubscribed=false`, гранта нет — если транзакция не устаревшая (иначе строка не меняется, см. выше).
- Невалидная/поддельная транзакция → `422`/`400` (тех. ошибка), подписка не меняется.
- StoreKit payload не логируется (redaction, [05-security.md](../../05-security.md)).
- **Test-mode (только e2e/CI, `STOREKIT_TEST_MODE=true`):** `transaction` принимается как HS256-JWS,
  подписанный `STOREKIT_TEST_SECRET`; извлекаются те же поля (`transactionId`/`expiresDate`/`productId`/
  …), активация и grant идут штатно. В prod (`STOREKIT_TEST_MODE=false`, дефолт) принимаются только
  реальные Apple-подписанные транзакции. См. [03-architecture.md](03-architecture.md#test-mode-верификации-storekit_test_mode), [TD-007](../../100-known-tech-debt.md).
