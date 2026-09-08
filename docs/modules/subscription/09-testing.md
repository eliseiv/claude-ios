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
- Ключ идемпотентности `sub-grant:{transaction_id}` не меняется: повтор транзакции не начисляет дважды и при активном оверлее.
- `archived: true` у подписочного продукта → продление **продолжает начислять**.
