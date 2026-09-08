# Token Purchase — Testing

## Unit
- Маппинг `productId → credits`; неизвестный → `422`.
- Число кредитов берётся из `TOKEN_PRODUCTS`, не из тела (подмена в теле игнорируется).

## Integration
- `POST /v1/tokens/purchase` (STOREKIT_TEST_MODE): валидная транзакция → grant, `creditsAdded`/`newBalance` корректны.
- Идемпотентность: повторная отправка той же транзакции → `creditsAdded=0`, баланс не растёт.
- Разграничение: token-purchase grant и subscription grant не конфликтуют (разные `meta.source`); subscription-grant поведение не меняется.
- Невалидная транзакция → `422`/`400`; `userId` ≠ `sub` → `403` (`code=forbidden`).
- **Policy-guard ([Q-015-1](../../99-open-questions.md) = вариант B):** нет активной подписки → `403 {code: "subscription_required"}`, **ledger не записан** (grant не вызван); активная подписка → grant проходит. Проверка подписки выполняется **до** verify/grant и не нарушает идемпотентность (повтор подписчика → `creditsAdded=0`).

## E2E
- Включить в [09-e2e-testing.md](../../09-e2e-testing.md): подписчик покупает пакет → рост баланса → списание в credits-mode. Отдельный кейс: без активной подписки покупка → `403 subscription_required`, баланс не меняется ([Q-015-1](../../99-open-questions.md) = вариант B).

## Каталог продуктов — признак «по умолчанию» ([ADR-098 §11](../../adr/ADR-098-broadapps-paywall-experiments-and-default-product.md))
- Источник — **наш** список `TOKEN_PRODUCTS_DEFAULT`, а не поле поставщика: продукт, чей
  `productId` в списке → `isDefault=true`; не в списке → `false`. Принимаются **обе** записи списка
  (JSON-массив и перечисление через запятую), лишние пробелы и пустые элементы отбрасываются.
- **Все три ветки источника** (живой каталог broadapps, `PRODUCTS_CATALOG`, `TOKEN_PRODUCTS`):
  поле присутствует всегда, и признак поднимается одинаково — кейс на каждую ветку, иначе «единая
  точка» проверена на одной из трёх.
- **Регресс на отменённый источник:** `is_default: true` в ответе поставщика **не** включает признак
  (поле у поставщика не читается вовсе). Кейс обязан падать при возврате чтения `is_default`.
- **Несколько помеченных продуктов** → у всех `isDefault=true`, **никакого WARNING**: снятое
  `token_products_multiple_defaults` не должно вернуться «по аналогии» с `isSpecialOffer`.
- Пустой / отсутствующий `TOKEN_PRODUCTS_DEFAULT` → у всех `false`, ничего не логируется.
- Регресс: `credits`/`price`/результат `POST /v1/tokens/purchase` от значения `isDefault` не зависят.

## Экономика инстанса из CRM ([ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md))

Полный перечень сценариев обеих поверхностей — [modules/admin/09-testing.md](../admin/09-testing.md#integration--экономика-и-настройки-инстанса-adr-099). Здесь — только путь этого модуля.

- Пустой оверлей: `POST /v1/tokens/purchase` начисляет **ровно** `TOKEN_PRODUCTS[productId]` — сегодняшнее число.
- Оверлей с другим `tokens` по тому же `productId` **побеждает env**; число берётся из него, не из тела запроса.
- Продукт, созданный оператором (`purchase_kind=one_time`), покупается и начисляет свои `tokens`.
- Продукта нет ни в env, ни в оверлее → **`422`** (BR-TP-1 не ослаблен оверлеем).
- `archived: true` → продукт **исчезает из `GET /v1/tokens/products`** (ветки 2 и 3), но покупка по нему **начисляет** как прежде.
- В ветке живого рублёвого каталога созданный оператором продукт **не появляется**; `credits`/`title` уже перечисленного продукта — из оверлея.
