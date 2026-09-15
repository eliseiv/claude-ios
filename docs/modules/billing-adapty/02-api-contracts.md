# billing-adapty / 02 — API Contracts

## POST /v1/billing/adapty/webhook

Серверный вебхук Adapty. **Вызывает Adapty**, не iOS-клиент. Контракт целиком в [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md); **реальный формат payload, маппинг событий и идемпотентность гранта исправлены в [ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)** (по реальным payload'ам Adapty); **резолв пользователя `customer_user_id` (deviceId→userId через `auth_devices`) — [ADR-055](../../adr/ADR-055-adapty-webhook-user-resolution-via-auth-devices.md)** (контракт эндпоинта не меняется, лечит `ignored/user_not_found` на реальном прод-флоу); **общий ключ гранта периода со StoreKit `sync`, резолв по `profile_id`, устаревшие события, незаведённый продукт и пакеты токенов `non_subscription_purchase` — [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md)** (путь, авторизация и форма тела ответа не меняются; добавлены две причины `ignored`).

### Авторизация
- `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` — статический секрет, заданный оператором в Adapty UI.
- НЕ пользовательский JWT, НЕ `X-Admin-Token`. Отдельный контур (третий тип авторизации; добавить в [API-REFERENCE §2](../../API-REFERENCE.md)).
- Сравнение constant-time (`hmac.compare_digest`). Неверный/нет токена → `401`. Секрет не сконфигурирован (`ADAPTY_WEBHOOK_SECRET` пуст) → `500`.

### Тело запроса
- **Без схемы / без Pydantic-валидации.** Читается сырое (`await request.body()`). Adapty при сохранении вебхука шлёт проверочный пинг с пустым/не-JSON/неполным телом — он обязан получить `2xx`.

#### Реальный формат payload (ADR-047, по факту прода)

Одна покупка генерирует **несколько** событий с **разными** `profile_event_id`, но **одним** `transaction_id`. Поля Adapty в wire-формате типично лежат в `event_properties` (`ep`); Dashboard-вид показывает их «расплющенными» (top-level). **Точная wire-структура (`event_type` плоский vs в обёртке) на 100% не подтверждена** — парсинг дефенсивный (см. [03-architecture.md](03-architecture.md)); финальная сверка — по логам [ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md) после деплоя.

Реальные ключевые поля (пример — недельная подписка `week_6.99_nottrial`, free-trial по промо `ytl`):
```json
{
  "event_type": "trial_started | access_level_updated | subscription_renewal_cancelled | subscription_started | subscription_renewed | subscription_expired | ...",
  "event_properties": {
    "profile_event_id": "a3254174-74a4-4597-82a0-83d9ebfd2cf0",
    "vendor_product_id": "week_6.99_nottrial",
    "subscription_expires_at": "2026-07-07T09:05:46Z",
    "transaction_id": 410003298316682,
    "original_transaction_id": 410003298316682,
    "is_active": true,
    "access_level_id": "premium",
    "will_renew": false,
    "profile_has_access_level": true,
    "profile_id": "3bf27b33-2866-4161-85c4-48bae895d7c4",
    "store": "app_store"
  }
}
```
- `event_id` нашего журнала ← **`profile_event_id`** (не `event_id`/`id` — их в payload нет).
- `customer_user_id` — идентификатор из `Adapty.identify`. **По факту прода iOS передаёт `deviceId`** (id устройства, [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)), а не наш JWT `userId`. Резолв — общий `resolve_user` ([ADR-055](../../adr/ADR-055-adapty-webhook-user-resolution-via-auth-devices.md)), см. §«Резолв пользователя» ниже.
- `profile_id` — Adapty-идентификатор профиля (UUID); присутствует в реальном payload (пример выше, `event_properties.profile_id`) и тогда, когда приложение не вызывало `Adapty.identify`. Второй идентификатор резолва ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §B): используется, если `customer_user_id` нет (или не UUID) либо он не резолвнут. Нет ни одного идентификатора → `200 ignored/missing_customer_user_id`.
- `transaction_id`/`original_transaction_id`/`profile_event_id` могут приходить **числом** (без кавычек) — парсер приводит к строке.

Полный порядок fallback-источников по каждому полю — [03-architecture.md §Дефенсивный парсинг](03-architecture.md).

#### Маппинг событий (ADR-047)

| `event_type` | Семантика | `subscriptions` | Кредиты |
|---|---|---|---|
| `trial_started` / `subscription_started` / `subscription_renewed` | GRANTING | `active`, `plan`, `expires_at` | **грант** (идемпотентно по `transaction_id`) |
| `access_level_updated` + `is_active=true` + `access_level_id="premium"` | GRANTING | `active`, `plan`, `expires_at` | **грант** |
| `subscription_expired` / `subscription_cancelled` | EXPIRING | `expired` | не трогаем |
| `access_level_updated` + `is_active=false` | EXPIRING | `expired` | не трогаем |
| `subscription_renewal_cancelled` / `trial_renewal_cancelled` | **NOOP** | **не трогаем** (доступ сохраняется) | не трогаем |
| `access_level_updated` + `is_active=true` + не-`premium` (или `is_active` неизвестен) | NOOP | не трогаем | не трогаем |
| `non_subscription_purchase` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E) | покупка пакета | **не трогаем** | **грант пакета** по `one_time_credits(vendor_product_id)`, ключ `token-purchase:{transaction_id}`; активная подписка **не** требуется |
| прочее (включая `subscription_refunded`, `non_subscription_purchase_refunded` — [TD-054](../../100-known-tech-debt.md)) | UNKNOWN | — | `200 ignored` (+эхо `event_type`) |

**NOOP** (отмена автопродления, `profile_has_access_level=true`, `will_renew=false`) — доступ **НЕ отзывается**; событие записывается (дедуп) + audit, но без мутации подписки/кредитов. Старые имена полей сохранены как fallback.

**Устаревшее событие ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §C).** GRANTING/EXPIRING/NOOP, чей `subscription_expires_at` **раньше** срока активной строки `subscriptions`, строку не меняет (срок назад не сдвигается, план не меняется, `expired` не ставится, `will_renew` не пишется); грант GRANTING решается ключом периода (§«Идемпотентность»), а не устареванием.

### Ответы

Все `200` кроме `401`/`500`. Тело: `{ "result": <...>, "reason"?: <...>, "event_type"?: <...> }`.

| HTTP | Тело | Когда |
|---|---|---|
| 401 | (ошибка авторизации) | нет/неверный bearer |
| 500 | (ошибка мис-конфигурации) | `ADAPTY_WEBHOOK_SECRET` не задан |
| 200 | `{"result":"ignored","reason":"empty_body"}` | пустое тело (проверочный пинг) |
| 200 | `{"result":"ignored","reason":"invalid_json"}` | не-JSON |
| 200 | `{"result":"ignored","reason":"not_an_object"}` | JSON не объект |
| 200 | `{"result":"ignored","reason":"missing_event_id"}` | нет `event_id` |
| 200 | `{"result":"ignored","reason":"missing_customer_user_id"}` | нет ни `customer_user_id` (UUID), ни `profile_id` (UUID) ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §B) |
| 200 | `{"result":"ignored","reason":"user_not_found"}` | идентификатор есть, но ни `customer_user_id`, ни `profile_id` не резолвнут `resolve_user` ([ADR-055](../../adr/ADR-055-adapty-webhook-user-resolution-via-auth-devices.md), [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §B) |
| 200 | `{"result":"ignored","event_type":"<echo>"}` | неизвестный `event_type` |
| 200 | `{"result":"ignored","reason":"missing_transaction_id"}` | `non_subscription_purchase` без `transaction_id` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E) |
| 200 | `{"result":"ignored","reason":"unknown_product"}` | `non_subscription_purchase`: `vendor_product_id` нет или продукт не разовый в каталоге инстанса ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E) |
| 200 | `{"result":"duplicate"}` | повтор `event_id` |
| 200 | `{"result":"applied"}` | событие применено |
| 500 | (внутренний сбой) | БД недоступна и т. п. → Adapty ретраит |

### Резолв пользователя ([ADR-055](../../adr/ADR-055-adapty-webhook-user-resolution-via-auth-devices.md))

Начисление/подписка/дедуп/audit ведутся на **резолвнутый** `userId`, не на исходный идентификатор. Резолв — общий модуль `src/app/billing_common/resolve.py::resolve_user(session, x)` (один для Adapty и CloudPayments), первое совпадение выигрывает: (a) `X∈users.id`→`(X,"user_id")`; (b) `lower(X)=lower(auth_devices.device_id)`→`(linked user_id,"device_id")`; (c) `X∈legacy_user_ids`→`(linked user_id,"legacy_user_id")` (ветвь идентификаторов прежнего сервиса; документа `ADR-096` нет — [TD-041](../../100-known-tech-debt.md)); (d) None.

**Порядок идентификаторов ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §B):** 1) `customer_user_id` → `resolve_user`; найден → `resolvedFrom="customer_user_id"`. 2) Иначе (нет или не найден) `profile_id` → `resolve_user`; найден → `resolvedFrom="profile_id"`. 3) Нет ни одного идентификатора → `missing_customer_user_id`. 4) Идентификатор был, ни один не резолвнут → `user_not_found`. Исходные идентификаторы сохраняются в логе (`customerUserId`, `profileId`); резолвнутый `userId` — реальный получатель гранта.

### Эффекты при `applied`
- GRANTING: `subscriptions.status=active`, `plan=vendor_product_id`, `expires_at` (из `subscription_expires_at`) — кроме устаревшего события ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §C); грант кредитов по тиру **один на период по любому каналу** (§«Идемпотентность»). Всё — на резолвнутый `userId` (ADR-055).
- EXPIRING: `subscriptions.status=expired` (кроме устаревшего события); кредиты не изменяются.
- NOOP (`*_renewal_cancelled`): подписка/кредиты **не изменяются** (доступ сохраняется); событие записано + audit.
- `non_subscription_purchase` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §E): `subscriptions` **не** читается и не пишется; грант пакета `one_time_credits(vendor_product_id)` под `token-purchase:{transaction_id}` — общим с `POST /v1/tokens/purchase`, поэтому пакет начисляется один раз, какой бы канал ни пришёл первым. Сумма и количество из payload **никогда** не читаются. Активная подписка **не** требуется — осознанное отличие от `POST /v1/tokens/purchase` ([Q-015-1](../../99-open-questions.md)): вебхук приходит после списания денег Apple.

### Идемпотентность (ADR-047 — разведены два механизма)
- **Дедуп события:** повтор `event_id` (= `profile_event_id`) → `duplicate` без побочных эффектов (UNIQUE `adapty_webhook_events.event_id`). Защищает от повторной доставки **того же** события.
- **Идемпотентность начисления ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A, заменяет ключ [ADR-047 §C](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md) при настоящем `transaction_id`):**

  | Событие | Ключ гранта | Проверяется до гранта |
  |---|---|---|
  | GRANTING, `transaction_id` есть | `sub-grant:{transaction_id}` (общий со StoreKit `POST /v1/subscription/sync`) | `sub-grant:{T}`, `adapty-txn:{T}` |
  | GRANTING, `transaction_id` нет | `adapty-txn:{original_transaction_id ‖ event_id}` (без изменений) | этот же ключ |
  | `non_subscription_purchase` | `token-purchase:{transaction_id}` (общий с `POST /v1/tokens/purchase`) | этот же ключ |

  Гарантирует **один грант на период / на покупку по любому каналу**, сколько бы событий ни пришло. `transaction_id` первичен; `original_transaction_id` НЕ первичен (постоянен на всю цепочку → продления не начисляли бы). **Занятый ключ = «уже начислено» независимо от суммы:** суммы каналов калибруются раздельно, и расхождение не даёт ни не-`2xx`, ни второго гранта — событие `applied` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A3). Сумма периода — у канала, чья строка записана первой. Инвариант действует в пределах одного `userId` ([ADR-106](../../adr/ADR-106-apple-billing-single-grant.md) §A5).
