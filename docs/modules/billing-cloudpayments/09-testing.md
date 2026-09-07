# billing-cloudpayments / 09 — Testing (ориентиры для qa)

**АКТУАЛЬНО — [ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md)** (публичный вебхук + верификация; [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md) резолв). Тесты герметичные — **исходящий `GET /users/{deviceId}/payments` мокируется** (без сети/реального broadapps). Стек/команды — [docs/02-tech-stack.md](../../02-tech-stack.md), [docs/06-testing-strategy.md](../../06-testing-strategy.md). Плейсхолдер-секрет `CLOUDPAYMENTS_API_TOKEN` в тестах (гейт активации).

## Авторизация ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) — публичный, нет 401)
- **Колбэк БЕЗ `Authorization` → НЕ `401`** (публичный): доходит до резолва пользователя/верификации. `authScheme="none"` в наблюдательном логе.
- Колбэк с любым `Authorization` (валидный легаси-токен / мусор) → тоже принимается (не блокирует).
- `CLOUDPAYMENTS_API_TOKEN==""` → **`500`** misconfigured (верификация невозможна ⇒ активен только avelyra).
- Per-source-IP флуд > `CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP` → `429` (fail-open при недоступности Redis — не блокирует).
- **Наблюдательный лог:** ровно один DEBUG/INFO `"cloudpayments_webhook_auth_observed"` (allowlist `matched`/`authScheme`/`presentAuthHeaders`; **нет** значения токена/заголовка). Прежнего `cloudpayments_webhook_auth_denied`/`401` **нет**. См. [08-observability §Auth-observed](08-observability.md).

## HTTP-контракт (всё `200 {"code":0}` кроме 429/500)
- Пустое тело / не-JSON / JSON-не-объект → `200 {"code":0}` (`ignored/empty_body`|`invalid_json`|`not_an_object`).
- `Status!="Completed"` или `OperationType!="Payment"` → `200 {"code":0}` (`not_a_completed_payment`).
- Нет/не-UUID `AccountId` и `Data.user_id` → `invalid_account_id`.
- `TransactionId`/`product_id` **отсутствуют** → колбэк **всё равно обрабатывается** (опц. контекст; не отсекают — регресс против ADR-050).
- verify `api_error` (мок timeout/5xx/malformed) → **`500` retriable**, начисления нет. broadapps `404` (мок) → `no_creditable_payment` (200), **не** 500.

## Резолв пользователя ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md), до verify)
- `X`(=`AccountId`/`Data.user_id`, верх → **lower**) в `users` → `resolvedVia="user_id"`, verify по `X`.
- `X` только в `auth_devices.device_id` (deviceId) → `userId=auth_devices[X].user_id`, `resolvedVia="device_id"`.
- `X` ни там ни там → `ignored/user_not_found` (WARNING), **исходящего GET НЕТ**, без создания пользователя/устройства.
- Карт-данные (`CardFirstSix`/…) **не** попадают в `ParsedPayment`/лог/`payload`.

## Верификация / реконсиляция ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md); мок `list_payments`)
- Мок `data=[{payment_id, status:"succeeded", product:{code, payment_type}, paid_at:<свежий>}]` → **начисление**: `applied`, `creditedCount=1`.
- `payment_type=="subscription"` → `subscriptions.status=active`, `plan=product.code`, `expires_at≈now+интервал` (unit из `code`); сумма = `CLOUDPAYMENTS_PRODUCT_TOKENS[code]` или fallback.
- `payment_type=="one_time"` → разовый грант `N=TOKEN_PRODUCTS[code]`, `subscriptions` **не** тронута.
- `status!="succeeded"` (напр. `pending`/`failed`) **или** `paid_at` вне окна свежести → **не начислен**; отбор пуст → `no_creditable_payment` (WARNING; лог `paymentStatuses`).
- Несколько свежих `succeeded` в `data[]` → начислены **все недоначисленные** (`creditedCount=len`), каждый идемпотентно.
- Неизвестный `product.code` (нет в картах) / неизвестный `payment_type` → платёж **пропущен** (WARNING `unknown_product`/`unknown_payment_type`), не начислен.
- `CLOUDPAYMENTS_PAID_STATUSES` (напр. добавлен `paid`) → соответствующий статус начисляется.

## Идемпотентность ([ADR-054](../../adr/ADR-054-cloudpayments-webhook-payment-verification.md) — по broadapps `payment_id`)
- Повтор колбэка → тот же `data` → **тот же `payment_id`** → `ON CONFLICT DO NOTHING` + ledger `cp-txn:{payment_id}` → `duplicate`, баланс/подписка не изменились (двойная граница).
- Продление: новый `payment_id` (тот же `subscription_id`) в `data[]` → **новый** грант + `expires_at` сдвинут.
- Гонка двух одинаковых колбэков → ровно один начисляет платёж, второй `duplicate` (ON CONFLICT по `transaction_id`=`payment_id`).
- Ключ дедупа/идемпотентности — broadapps `payment_id`, **не** callback `TransactionId`.

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

## Эксперименты пейволла ([ADR-098](../../adr/ADR-098-broadapps-paywall-experiments-and-default-product.md))
- **Идентичность:** исходящее тело содержит `user_id == JWT sub`; попытка передать `userId`/`deviceId`/`appId` в теле → `422` (StrictModel). Тест обязан падать, если реализация начнёт брать идентификатор из тела или из claim `device_id`.
- **Подстановка сервера:** `app_id` = `CLOUDPAYMENTS_APP_ID`, `context.platform == "ios"`, `context.paywall.placement` = переданный `placement` **дословно** (кейс `onbording` — не нормализуется), `context.locale` — из `Accept-Language` (`ru-RU` → `ru`), при отсутствии заголовка — из `PRESETS_DEFAULT_LOCALE`, иначе `en`. Локаль **не** клампится к набору локалей каталогов (кейс `de` → `de`, а не `en`).
- **Кодировка:** исходящий запрос — `application/json` (регресс: multipart, как у `/payments/link`, недопустим — `context` вложенный).
- **`assign`:** 2xx с `assignment.segment.code` → `200` с полями нашей схемы; `requested_segment_matches=false` пробрасывается; таймаут/сеть/не-2xx/2xx-без-сегмента → `502 upstream_error`, тело/статус/токен поставщика наружу **не** попадают; сегмент **не подставляется** ни при каком отказе.
- **`paywall-shown`:** 2xx → `200 {"logged": true}`; **любой** отказ поставщика → `200 {"logged": false}`. Регресс-тест: ни один отказ не даёт `502` (тест обязан падать, если ветку «никогда не 502» убрать).
- **Гейт:** пустой `CLOUDPAYMENTS_APP_ID` или `CLOUDPAYMENTS_API_TOKEN` → обе ручки `503 cloudpayments_checkout_not_configured`, исходящего вызова нет.
- **Лимит:** превышение корзины `rl:experiments:{user_id}` → `429`; изоляция — исчерпание этой корзины **не** влияет на `POST /v1/billing/cloudpayments/checkout` (и наоборот). Это ключевой регресс-тест: он обязан падать при возврате к общему `enforce_other_limits`.
- **Изоляция:** ни одна запись в БД (ledger/subscriptions/wallet/webhook-events) по этим вызовам не появляется.
