# Billing (Adapty) — Testing

Сценарии вебхука Adapty. Общие сценарии обеих admin-поверхностей —
[modules/admin/09-testing.md](../admin/09-testing.md#integration--экономика-и-настройки-инстанса-adr-099).

## Экономика инстанса из CRM ([ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md))

- Пустой оверлей: `subscription_started`/`renewed` начисляет `ADAPTY_PRODUCT_TOKENS[vendor_product_id]`,
  а при отсутствии в карте — `ADAPTY_SUBSCRIPTION_TOKENS_GRANT`. Сегодняшнее поведение.
- Оверлей с `purchase_kind=subscription` по тому же `vendor_product_id` → начисляется его `tokens`
  (**побеждает env-карту**).
- Ключ идемпотентности гранта (`adapty-txn:{transaction_id}`, [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md))
  и дефенсивный парсинг события не меняются при активном оверлее.
- Архивный продукт: продление **продолжает начислять** — архив снимает продукт с витрины, а не
  запрещает операции.
