# ADR-057 — Фолбэк классификации при рассинхроне `product.payment_type` у провайдера + отделение `skipped` от `duplicate`

- Статус: Accepted
- Дата: 2026-07-20
- Тип: bugfix-ADR, **пересматривает [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) §3** (классификация платежа **только** по `product.payment_type`) и **§Наблюдаемость** (агрегатный исход). Восстанавливает симметрию checkout ↔ вебхук, заявленную в [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) §2.
- Связано: [ADR-050](ADR-050-cloudpayments-webhook.md) (паттерн-классификация `classify_product`, которую ADR-054 вывел из пути начисления), [ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)/[ADR-055](ADR-055-adapty-webhook-user-resolution-via-auth-devices.md) (резолв — не затронут), [ADR-005](ADR-005-idempotency-ledger.md) (идемпотентность гранта), [ADR-048](ADR-048-admin-subscription-grant.md) (ретроактивная компенсация). Модуль [billing-cloudpayments](../modules/billing-cloudpayments/README.md).

## Context

**Инцидент (прод avelyra, 2026-07-19, подтверждён логами + БД + прямым запросом к broadapps API): подписка оплачена, начисления нет.**

Тестировщик оплатил недельную подписку. Платёж прошёл (`status: succeeded`), вебхук дошёл, пользователь резолвнулся, верификация отработала — и платёж был **молча выброшен**:

```
WARNING cloudpayments_payment_skipped reason="unknown_product"
        productId="week_6.99_nottrial" paymentType="one_time"
        userId="5319b632-d4af-4203-824e-520be2eaf419"
INFO    cloudpayments_webhook_outcome result="duplicate" verify="ok"
        creditedCount=0 paymentStatuses=["succeeded"]
```

Состояние в БД: `wallets.balance = 0`, ноль строк в `ledger_transactions`, строки в `subscriptions` нет.

Ответ broadapps API (источник истины по [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md)) на момент разбора:

```json
{"payment_id":"31eed623-000f-5001-8000-110f5a408722","status":"succeeded",
 "amount":"1.00","currency":"RUB","is_recurring":false,"is_tester":true,
 "product":{"code":"week_6.99_nottrial","name":"Недельная подписка","payment_type":"one_time"},
 "subscription_id":null}
```

**Корень бага — рассинхрон конфигурации продукта у провайдера, который наш код не переживает.** Продукт `week_6.99_nottrial` — недельная подписка (так он называется и так продаётся), но broadapps отдаёт по нему `payment_type: "one_time"` (продукт перенастроили из-за временной поломки подписок на стороне провайдера). Дальше срабатывает [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) §3 буквально:

1. `payment_type == "one_time"` → класс **токены** (`service.py:247`).
2. Сумма ищется в `TOKEN_PRODUCTS` — карте **консьюмеблов** (`100_tokens_9.99`, `250_tokens_19.99`, …). Кода `week_6.99_nottrial` там нет и быть не должно.
3. → `unknown_product` → `return False` → оплаченный платёж потерян.

Что делает баг системным, а не разовым:

- **Нарушен инвариант, который код декларирует сам.** `checkout.validate_product` ([ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) §2, `checkout.py:50-62`) выдаёт ссылку на оплату **только для продукта, который вебхук сможет начислить**, и классифицирует его через `parser.classify_product` — по коду. Для `week_6.99_nottrial` checkout говорит `subscription` и выдаёт ссылку; вебхук говорит `unknown_product` и выбрасывает платёж. Пользователь платит по ссылке, которую мы сами выписали, и не получает ничего.
- **[ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) вывел `classify_product` из пути начисления** («`classify_product` в начислении не используется, остаётся для checkout»), заменив паттерн-эвристику авторитетным `payment_type`. Это было правильно как anti-tamper-решение, но убрало **последнюю страховку** на случай, когда авторитетный источник сам рассинхронизирован.
- **Регрессия невидима.** Тот же продукт начислял 1000 токенов 03.07 и 17.07 (`kind=subscription` в `cloudpayments_webhook_events`, гранты в леджере). Между 17.07 и 19.07 `payment_type` сменился, и с этого момента каждая покупка подписки теряется.

**Отдельный дефект наблюдаемости, из-за которого инцидент нашёл тестировщик, а не алерт.** Агрегатный исход считался по одному счётчику `credited` (`service.py:219-223`): `credited >= 1 → applied`, иначе `duplicate`. Платёж, **пропущенный** (пп. 1-3), давал `result="duplicate"` при `creditedCount: 0` — в логах это неотличимо от штатной повторной доставки, уровень INFO, ни одного WARNING на агрегате. Реальный сигнал был только в отдельной строке `cloudpayments_payment_skipped`, по которой алерта нет.

## Decision

Две независимые части: **(A)** фолбэк классификации там, где платёж иначе теряется; **(B)** отделить `skipped` от `duplicate` в агрегатном исходе.

### A. Фолбэк: re-классификация по коду перед потерей платежа

В `_apply_payment`, **только** на ветке, которая сегодня возвращает `unknown_product`:

```python
if kind == parser.KIND_TOKENS and payment.product_code not in token_products:
    if parser.classify_product(payment.product_code, None, frozenset(token_products))
       == parser.KIND_SUBSCRIPTION:
        kind = parser.KIND_SUBSCRIPTION
        log_event(logger, logging.WARNING, "cloudpayments_payment_type_mismatch", ...)
```

Дальше — обычный путь подписки: сумма из `CLOUDPAYMENTS_PRODUCT_TOKENS[code]` или фолбэк `CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT`, upsert `subscriptions` со сроком из `infer_interval_unit_from_code` (`week` → 7 дней), грант идемпотентно по `cp-txn:{payment_id}`.

Обоснование выбора:

- **Переиспользуем ту же функцию, что и checkout, а не новую эвристику.** `classify_product` — чистая функция ([ADR-050](ADR-050-cloudpayments-webhook.md) §3), уже являющаяся источником истины для allowlist checkout. Фолбэк буквально означает: «если мы выписали ссылку на этот продукт как на подписку, то и начислим его как подписку». Симметрия восстановлена в обе стороны.
- **Anti-tamper [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) §6 не ослаблен.** `product.code` по-прежнему берётся **только** из верифицированного ответа broadapps, никогда из тела колбэка; сумма — по-прежнему **только** из серверных карт. Форжед-колбэк по-прежнему максимум триггерит бесполезный GET. Меняется лишь трактовка поля, которое пришло из того же доверенного ответа.
- **Нулевой риск регрессии.** Ветка достижима только там, где платёж сегодня **гарантированно теряется** (`one_time` + кода нет в `TOKEN_PRODUCTS`). Ни один платёж, который начисляется сегодня, не меняет поведение.
- **Самоочищается.** Когда broadapps вернёт `payment_type: subscription`, фолбэк перестанет срабатывать сам, без правки конфигурации и деплоя.

**Границы (что фолбэк НЕ делает):**

- Не выдумывает консьюмебл: код, похожий на токен-продукт (`^\d+_tokens`, напр. `999_tokens_pack`), но отсутствующий в `TOKEN_PRODUCTS`, остаётся `unknown_product`. Сумму разового пакета угадывать нельзя — это операторская ошибка конфигурации, а не рассинхрон провайдера.
- Не трогает `unknown_payment_type` (`payment_type` вне `{one_time, subscription}`) — там класс не определён вовсе.
- Не трогает обратный случай (`subscription`-платёж по токен-коду): он начисляется по карте подписок и платёж не теряется.

**Альтернатива — явный env-оверрайд `CLOUDPAYMENTS_FORCE_SUBSCRIPTION_PRODUCTS` — отвергнута** как основная: требует правки `.env` + рестарта на каждый продукт, не самоочищается после починки на стороне провайдера и создаёт второй, конкурирующий источник истины о классе продукта рядом с `classify_product`.

### B. `skipped` ≠ `duplicate` в агрегатном исходе

`_apply_payment` возвращает `Literal["credited", "duplicate", "skipped"]` вместо `bool`; `handle()` считает две величины:

| Условие | Исход | Уровень |
|---|---|---|
| `credited >= 1` | `applied` | INFO |
| иначе `skipped >= 1` | `ignored` / `payment_skipped` | **WARNING** |
| иначе | `duplicate` | INFO |

Новая причина `payment_skipped` добавлена в WARNING-набор `_level_for` рядом с `user_not_found` / `no_creditable_payment` — это тот же класс инцидента («колбэк пришёл, начислить не смогли»), и теперь он виден на агрегате, по которому строится алертинг. Смешанная пачка (что-то начислено, что-то пропущено) остаётся `applied`: пропуск виден отдельной per-payment WARNING.

## Consequences

**Положительные.** Оплаченная подписка начисляется даже при рассинхроне `payment_type` у провайдера. Восстановлена симметрия checkout ↔ вебхук ([ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) §2). Потерянный платёж перестаёт маскироваться под `duplicate` и становится алертируемым. Рассинхрон провайдера громко логируется (`cloudpayments_payment_type_mismatch`, WARNING) — его видно и можно закрыть на стороне broadapps, после чего фолбэк молча выключится сам.

**Отрицательные / риски.**

- Класс платежа для одного узкого случая снова зависит от **имени** продукта, а не от авторитетного поля. Смягчение: срабатывает только там, где альтернатива — потеря платежа; та же функция уже гейтит checkout.
- Продукт с подписочным именем, задуманный как разовая покупка и не заведённый в `TOKEN_PRODUCTS`, теперь получит подписку вместо `unknown_product`. На практике такой продукт и через checkout продавался бы как подписка (тот же `classify_product`), так что расхождения с выписанной ссылкой не возникает.
- Изменение исхода `duplicate` → `ignored/payment_skipped` — **поведенческое** для потребителей лога/значения `WebhookOutcome`. HTTP-контракт эндпоинта не меняется (по-прежнему `200 {"code": 0}`), но дашборды/алерты, считающие `duplicate`, увидят сдвиг.

**Без миграций.** Схема БД, контракт эндпоинта, HTTP-семантика, резолв пользователя, идемпотентность (`cp-txn:{payment_id}`), окно свежести, rate-limit, Adapty- и StoreKit-пути — не затронуты. Обратная совместимость по данным полная.

**Ретроактивные операторские действия (вне backend-кода).** Платежи, потерянные с момента рассинхрона, кодом не восстанавливаются — фолбэк работает только на новых доставках, а по потерянным dedup-строка не создавалась. Компенсация — через [ADR-048](ADR-048-admin-subscription-grant.md) (`POST /v1/admin/wallet/grant` с устойчивым `idempotencyKey`). Подтверждённый пострадавший: `5319b632-d4af-4203-824e-520be2eaf419` (avelyra, платёж `31eed623-000f-5001-8000-110f5a408722`). Полный список — сверкой платежей broadapps по `auth_devices` с `ledger_transactions`; окно логов контейнера (с 18.07) короче окна инцидента, поэтому лог не является исчерпывающим источником.

**Файлы backend:** `src/app/billing_cloudpayments/service.py`. Тесты: `tests/integration/test_billing_cloudpayments_payment_type_fallback_adr057.py` (новый), правки в `test_billing_cloudpayments_verification_adr054.py` (исход пропуска) и `test_billing_cloudpayments_service_reasons.py` (уровень `payment_skipped`).

## Открытые вопросы

- **Q-057-1.** Является ли `payment_type: one_time` у `week_6.99_nottrial` временной поломкой на стороне broadapps или новой постоянной конфигурацией? От ответа зависит, снимать ли фолбэк после починки или оставить как постоянную страховку. Наблюдение — по частоте `cloudpayments_payment_type_mismatch`.
- **Q-057-2.** Влияет ли тестовый режим (`is_tester: true`, `amount: 1.00`, `subscription_id: null`) на `payment_type` независимо от конфигурации продукта. Если да — реальные покупки могли не пострадать, и оценка ущерба сузится до тестовых платежей.
