# Billing (Adapty) — Testing

Сценарии вебхука Adapty. Общие сценарии обеих admin-поверхностей —
[modules/admin/09-testing.md](../admin/09-testing.md#integration--экономика-и-настройки-инстанса-adr-099).

## Экономика инстанса из CRM ([ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md))

- Пустой оверлей: `subscription_started`/`renewed` начисляет `ADAPTY_PRODUCT_TOKENS[vendor_product_id]`,
  а при отсутствии в карте — `ADAPTY_SUBSCRIPTION_TOKENS_GRANT`. Сегодняшнее поведение.
- Оверлей с `purchase_kind=subscription` по тому же `vendor_product_id` → начисляется его `tokens`
  (**побеждает env-карту**).
- Ключ идемпотентности гранта ([ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md); с [ADR-106 §A](../../adr/ADR-106-apple-billing-single-grant.md) при настоящем `transaction_id` — `sub-grant:{transaction_id}`)
  и дефенсивный парсинг события не меняются при активном оверлее.
- Архивный продукт: продление **продолжает начислять** — архив снимает продукт с витрины, а не
  запрещает операции.

## Одна оплата — одно начисление ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md))

Integration на реальной БД. Каждый кейс обязан падать при мутации ТОГО инварианта, который он стережёт; мутационная пара — по инварианту, а не одна на раздел. Кейсы StoreKit-стороны — [subscription/09-testing.md](../subscription/09-testing.md#одна-оплата--одно-начисление-adr-106), `POST /v1/tokens/purchase` — [token-purchase/09-testing.md](../token-purchase/09-testing.md#одна-оплата--одно-начисление-adr-106).

### A. Один период — один грант
- `sync` по `T` → вебхук GRANTING по `T` → ровно одна строка `credit`, вебхук `applied`.
- Вебхук по `T` → `sync` по `T` → одна строка, `sync` `200`.
- Параллельно `sync` и вебхук по `T` → одна строка; ни `409`, ни не-`2xx`. Фикстура задаёт **разные** суммы каналов (`SUBSCRIPTION_CREDITS_PER_PERIOD` ≠ `ADAPTY_PRODUCT_TOKENS[pid]`): на равных суммах конфликт суммы не воспроизводится.
- Разные суммы, последовательно: сумма = у первого канала, второй завершается штатно.
- Историческая строка `adapty-txn:{T}` → вебхук и `sync` по `T` не начисляют.
- Историческая строка `sub-grant:{T}` → вебхук по `T` не начисляет.
- Продление (новый `T'`, тот же `original_transaction_id`) → новый грант.
- GRANTING без `transaction_id` → ключ `adapty-txn:{original_transaction_id}`, как прежде.
- Два granting-события одного `T` с разными `profile_event_id` → одна строка (регресс ADR-047).

### B. Резолв по `profile_id`
- Только `profile_id`, совпадающий с `auth_devices.device_id` **в другом регистре** → `applied`, грант на связанного `user_id`, `resolvedFrom="profile_id"`.
- Оба идентификатора резолвимы на разных пользователей → побеждает `customer_user_id`.
- `customer_user_id` есть, но не резолвнут; `profile_id` резолвнут → `applied`, `resolvedFrom="profile_id"`.
- `customer_user_id` не UUID + резолвимый `profile_id` → `applied` по `profile_id`.
- Ни одного идентификатора → `ignored/missing_customer_user_id`; оба есть и оба не резолвнуты → `ignored/user_not_found` (WARNING, `profileId` в логе).

### C. Устаревшее событие
- Активная строка со сроком `S`; GRANTING со сроком `< S` → `plan`/`expires_at` не изменились; грант по ключу: период не начислен → начислен, начислен → нет; audit `stale: true`.
- EXPIRING со сроком `< S` → строка осталась `active`.
- NOOP со сроком `< S` → `will_renew` не изменился.
- GRANTING со сроком `> S` (продление) и `= S` (повтор периода) → обрабатываются как прежде (кейсы против переоценки).
- Строка `expired` + GRANTING с более ранним, но будущим сроком → строка обновлена (предикат требует `active`).

### D. Незаведённый продукт
- `vendor_product_id` нет ни в оверлее, ни в `ADAPTY_PRODUCT_TOKENS` → грант = `ADAPTY_SUBSCRIPTION_TOKENS_GRANT`, ровно одна запись WARNING `subscription_product_unmapped` в захваченном журнале и одно событие аудита.
- Продукт в карте или в оверлее → записи нет.
- Повтор того же `T` (грант не создан) → записи нет.

### E. `non_subscription_purchase`
- Разовый продукт в каталоге, пользователь **без** подписки → `applied`, баланс вырос на `one_time_credits`, строка `subscriptions` не создана и не изменена.
- Вебхук по `T` → `POST /v1/tokens/purchase` по `T` (подписчик) → одна строка, ответ `creditsAdded=0`.
- `POST /v1/tokens/purchase` по `T` → вебхук по `T` → одна строка, вебхук `applied`.
- Нет `transaction_id` → `ignored/missing_transaction_id`, WARNING, строк в `adapty_webhook_events` и ledger нет.
- Продукт вне каталога / подписочный продукт / нет `vendor_product_id` → `ignored/unknown_product`, WARNING, баланс не изменился.
- Количество/сумма в payload отличается от каталога → начислено число каталога (анти-тампер).
- Повтор `profile_event_id` → `duplicate`.
- `non_subscription_purchase_refunded` → `ignored` с эхом типа, баланс не изменился ([TD-054](../../100-known-tech-debt.md)).
