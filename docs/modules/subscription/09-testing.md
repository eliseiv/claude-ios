# Subscription — Testing

## Unit
- Нормализация статуса: expiresDate в прошлом/будущем, revoked → active/expired.

## Integration (respx для App Store Server API)
- Валидная транзакция → status=active, expiresAt, plan; upsert корректен.
- Поддельная/невалидная подпись → 422, subscription не изменена.
- Повторный sync той же транзакции → grant не дублируется (идемпотентность по transactionId).
- refund/revocation → status=expired, isSubscribed=false.
- Истёкшая подписка → Policy Engine отдаёт subscription_expired для chat (интеграция с AC-2).
- audit subscription_change создаётся.

## Экономика инстанса из CRM ([ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md))

Полный перечень сценариев обеих поверхностей — [modules/admin/09-testing.md](../admin/09-testing.md#integration--экономика-и-настройки-инстанса-adr-099). Здесь — только путь этого модуля.

- Пустой оверлей: `/v1/subscription/sync` начисляет `SUBSCRIPTION_CREDITS_PER_PERIOD` — сегодняшнее поведение, независимо от `productId`.
- Оверлей с `purchase_kind=subscription` по `productId` верифицированной транзакции → начисляется его `tokens` (это **единственный** путь, где продукт раньше на сумму не влиял).
- Ключ идемпотентности `sub-grant:{transaction_id}` не меняется: повтор транзакции не начисляет дважды и при активном оверлее. (Ключ стал общим с вебхуком Adapty — [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md).)
- `archived: true` у подписочного продукта → продление **продолжает начислять**.

## Одна оплата — одно начисление ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md))

Integration на реальной БД; кейсы вебхука — [billing-adapty/09-testing.md](../billing-adapty/09-testing.md#одна-оплата--одно-начисление-adr-106). Мутационная пара — на каждый инвариант.

- `sync` по `T` после гранта вебхука по `T` → строка ledger одна, ответ `200`.
- Историческая строка `adapty-txn:{T}` → `sync` по `T` не начисляет.
- Строка под `sub-grant:{T}` с **другой** суммой (записана вебхуком) → `sync` отвечает `200`, не `409`, второй строки нет.
- Новый `T'` (продление) → начисляет.
- Активная подписка со сроком `S`, транзакция со сроком `< S` → `plan`/`expires_at` не изменились, ответ = состояние строки, audit `stale: true`; грант: `T` не начислен → начислен, начислен → нет.
- Транзакция со сроком `> S` → строка обновлена (кейс против переоценки).
- `isUpgraded=true` → `isSubscribed=false`, гранта нет, audit `upgraded: true`; тот же payload без флага → активна (кейс обязан падать при снятии чтения `isUpgraded`).
- `productId` вне каталога инстанса, оверлея нет → грант `SUBSCRIPTION_CREDITS_PER_PERIOD`, одна запись WARNING `subscription_product_unmapped` (`channel="storekit"`) в захваченном журнале и одно событие аудита; `productId` из env-карты (например `ADAPTY_PRODUCT_TOKENS`) без оверлея → записи нет; повтор `sync` → записи нет.
