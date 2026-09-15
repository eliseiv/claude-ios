# Token Purchase — Architecture

## Размещение
Пакет `src/app/token_purchase/`: use-case `purchase_tokens` + роутер `/v1/tokens/*`. Verifier — общий с subscription (выделить в общий модуль `src/app/storekit/` или переиспользовать существующий verifier subscription без дублирования).

## Поток purchase
```
require_active_subscription(userId)                    # Q-015-1=B: SubscriptionService/policy; нет active -> 403 subscription_required
verify(transaction) -> { transactionId, productId }    # общий verifier (fail-closed; STOREKIT_TEST_MODE)
credits = TOKEN_PRODUCTS[productId]                     # неизвестный -> 422
Wallet.grant(credits, idempotency_key="token-purchase:{transactionId}",
             type=credit, meta={source:"token_purchase", productId, transactionId})
-> { creditsAdded, newBalance }
```

## Проверка подписки (обязательная, [Q-015-1](../../99-open-questions.md) = вариант B)
- **Где:** в use-case `purchase_tokens`, **первым шагом — до verify и до `Wallet.grant`**. Источник истины статуса подписки — `SubscriptionService`/policy-слой (тот же, что питает `PolicyState.subscription_status`, [ADR-002](../../adr/ADR-002-access-policy-state-machine.md)); проверяем `subscription.status == active`.
- **Почему до verify:** fail-fast — не расходуем вызов App Store Server API и не пишем ledger для неподписанного пользователя.
- **Почему до grant:** policy-guard не должен ломать идемпотентность. Проверка подписки чисто read-only и предшествует единственной записи ledger (`Wallet.grant`). Если подписки нет — grant не вызывается, `ux_ledger_idempotency` не затрагивается.
- **Отказ:** HTTP `403`, тело `{ "code": "subscription_required", "message": "..." }`. Это операция пополнения, не генерация → `403`, а не `200+blocked` ([ADR-004](../../adr/ADR-004-blocked-http-200.md) применяется только к `/chat/*` и `/policy/effective`; см. [ADR-015 §Код ответа](../../adr/ADR-015-consumable-token-iap.md)).

## Идемпотентность ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-015](../../adr/ADR-015-consumable-token-iap.md))
- `idempotency_key = "token-purchase:{transactionId}"` (`_IDEMPOTENCY_PREFIX`, `src/app/token_purchase/service.py`). Unique-index `ux_ledger_idempotency (user_id, idempotency_key)` гарантирует один grant на транзакцию. Повтор → idempotent-ответ (`creditsAdded=0`).
- **Второй производитель ключа** — вебхук Adapty `non_subscription_purchase` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E): тот же ключ, та же сумма `one_time_credits`. Порядок каналов не важен; строка под ключом = «уже начислено» (`creditsAdded=0`) независимо от суммы — расхождение сумм (правка оверлея между каналами) не даёт `409` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A3). Уникальность — в пределах `userId`.
- Префиксы `sub-grant:` (подписка) и `token-purchase:` (пакет) различны — пространства не пересекаются.
- ⚠️ **Контраст порядка проверок:** здесь policy-guard подписки идёт **первым** и отказывает `403`; вебхук Adapty проверки подписки не выполняет вовсе ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E3). Переносить guard в вебхук запрещено: там деньги уже списаны.

## Разграничение с subscription
- Subscription verifier и grant — не меняются (ADR-006). Token-purchase — отдельный endpoint и отдельный путь grant с тем же Wallet API.
- Списание (`type=debit`) — не затрагивается; «1 кредит = 1 сообщение» в силе.

## Инварианты
- **Покупка через этот endpoint только при активной подписке** ([Q-015-1](../../99-open-questions.md) = вариант B): grant не выполняется без `subscription.status == active`. Вебхук Adapty — отдельный канал без этого условия ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E3).
- Число кредитов — только из серверного каталога (`one_time_credits`: оверлей → `TOKEN_PRODUCTS`), никогда из тела клиента или payload вебхука.
- Wallet — единственный писатель ledger (инвариант сохранён).
- StoreKit payload не логируется.

## Источник числа кредитов после [ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md)

Число кредитов, начисляемых по продукту, берётся **единым резолвером** в порядке
**оверлей `admin_products` → env-карта этого канала → фиксированный грант канала**. Оверлей
заполняется оператором из CRM (`POST`/`PATCH /v1/admin/products`), лежит в БД инстанса и **всегда
серверный**: анти-тампер не ослабляется — число кредитов по-прежнему **никогда** не приходит из
тела пользовательского запроса.

- **Пустая таблица оверлея = сегодняшнее поведение бит-в-бит.** Ни один продукт, заведённый в env,
  не меняет суммы начисления этим выкатом.
- **Правка применяется не мгновенно:** значение читается из снимка процесса, обновляемого раз в
  `ADMIN_OVERRIDES_REFRESH_SECONDS` (дефолт 30 с). Окно объявляется оператору полем
  `effective_after_seconds` admin-контракта.
- **Архивный продукт (`archived: true`) начисляет как обычно.** Архив снимает продукт **с
  витрины** приложения и не является запретом операций — иначе он ломал бы уже оплаченное и
  активные подписки.
- ⚠️ **После правки величины из CRM правка `.env` по ней ничего не меняет** — оверлей приоритетнее
  env ([ADR-099 §2](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md)).
- ⛔ **«Строка оверлея есть» ≠ «оверлей задал это поле».** Колонки `name`/`purchase_kind`/`tokens`
  — nullable со смыслом «оверлей этого поля не задаёт»
  ([ADR-099 §6.1](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md)), поэтому и
  резолвер начисления, и витрина сливают строку **пофилдово**: оверлей побеждает только там, где
  значение задано, иначе читается источник. Строка, созданная правкой одного `archived`, не несёт
  ни числа, ни класса — и **не имеет права** превратить продукт в «неизвестный» (`422` на покупке)
  или обнулить грант канала: это было бы изменением начисления правкой, его не касавшейся, что
  запрещено несущим свойством волны (§2). Класс `NULL` вдобавок **не снимает** продукт с разового
  множества — оверлей о классе не высказывается.

Для этого модуля путь — `one_time`: `_credits_for_product` спрашивает резолвер, а не
`settings.token_products()` напрямую. **Неизвестный `productId` по-прежнему `422`** (BR-TP-1):
оверлей расширяет каталог, но не отменяет отказ по продукту, которого в каталоге нет.
