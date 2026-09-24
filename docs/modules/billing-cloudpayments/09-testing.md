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

## Пути-дубликаты `/v1/web/*` ([ADR-110 §8](../../adr/ADR-110-ru-payment-neutral-path-aliases.md))
Обязательное покрытие — по [ADR-110 §8](../../adr/ADR-110-ru-payment-neutral-path-aliases.md) целиком; кратко:
- паритет КАЖДОЙ из пяти пар (одинаковый вход → одинаковые статус и тело, ветви успеха и отказов);
- вебхук `/v1/web/events`: без `Authorization` не `401`, поддельный колбэк не начисляет, пустой `CLOUDPAYMENTS_API_TOKEN` → `500`, кривое тело → `200 {"code":0}`;
- одна корзина на пару (`rl:cpwebhook`, `rl:other`, `rl:experiments`) при чередовании путей;
- один платёж на оба пути → одно начисление;
- `/openapi.json` содержит все пять путей `/v1/web/*`; у каждой операции дубликата тег, `summary`, `description`, схемы запроса/ответа и security совпадают с операцией оригинала, `operationId` пары различны; операции оригиналов не изменились ([ADR-110 §2](../../adr/ADR-110-ru-payment-neutral-path-aliases.md));
- `POST .../cancel` (до ADR-110 без автотестов): нет активной подписки → `canceled=false`; отказ поставщика → `502`; успех → `will_renew=false` без смены `status`/`expires_at`;
- `POST .../cancel` при `canceled=false` и СУЩЕСТВУЮЩЕЙ локальной строке `subscriptions` с `will_renew=true` (в т. ч. подписки Apple/Adapty): ЗАКРЕПЛЯЕТСЯ — `will_renew` остаётся `true`, ответ `willRenew=true`; на обоих путях пары ([ADR-111](../../adr/ADR-111-ru-cancel-will-renew-only-on-found.md)); `canceled=false` без строки → `willRenew=false`, строка не создаётся; возврат безусловной записи обязан уронить кейс;
- снятие регистрации любого дубликата роняет хотя бы один кейс.

## Страница оплаты на домене инстанса ([ADR-113 §8](../../adr/ADR-113-ru-payment-page-proxy-on-instance-domain.md))
Обязательное покрытие — по [ADR-113 §8](../../adr/ADR-113-ru-payment-page-proxy-on-instance-domain.md) целиком (29 кейсов, каждый отдельным тестом; upstream — подменённый транспорт httpx). Кратко:
- переписывание `paymentUrl` на ОБОИХ путях пары (`/v1/billing/cloudpayments/checkout`, `/v1/web/session`): переписано при всех четырёх условиях; не тронуто при ложном любом из них (с WARNING `rewrite_skipped` и верным `reason`) и для YooMoney / T-Банк (без WARNING); хост сравнивается как хост (`xpay.…`, `….evil.test` — не переписываются); удаление вызова переписывания роняет тест;
- прокси: белый список по сырому пути (`..`, `%2e`, `//`, `%2F` → `404` без исходящего вызова); query, не собираемый в URL upstream (не-ASCII байт, `#`), → тот же `404` без исходящего вызова и без лога прокси; хост upstream не зависит от `Host`/`X-Forwarded-Host`; `Authorization`/`X-Forwarded-*` не уходят upstream; замена хоста во ВСЕХ формах записи, включая `https:\/\/` и `%2F`/`%2f`, и отказ от замены в `xpay.…`, `….dev.evil.test`, `….devx`, `a.pay.…`; `Set-Cookie` без `Domain`; `Location`; статусы upstream как есть; `502` на таймаут/соединение/> 5 MiB; `429` в своей корзине `rl:cppage`; `404` при флаге `false` на каждом из четырёх маршрутов и при ненастроенном checkout — одинаковым ответом, без исходящего вызова; `rewrite_skipped/disabled` — INFO, `path_not_proxied`/`service_domain_unset` — WARNING; лог без uuid/query/cookie; access-лог с `/cp/pay/*` вместо uuid и без query — отдельно на входе `/cp/pay/<uuid>` БЕЗ query, с diff-стойкостью именно на нём; заголовки безопасности (наши HSTS/XFO/nosniff ровно по одному разу, CSP upstream передан с заменой хоста, неизвестный заголовок отброшен и назван в `droppedHeaders` без значения); `HEAD` без `Content-Length`; путей прокси нет в OpenAPI;
- ручная проверка реальным платежом ([ADR-113 §6](../../adr/ADR-113-ru-payment-page-proxy-on-instance-domain.md) п. 2) автотестом не заменяется.

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

## Экономика инстанса из CRM ([ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md))

Полный перечень сценариев обеих поверхностей — [modules/admin/09-testing.md](../admin/09-testing.md#integration--экономика-и-настройки-инстанса-adr-099). Здесь — только путь этого модуля.

- Ветка `KIND_TOKENS`: пустой оверлей → `TOKEN_PRODUCTS[product_code]`; оверлей → его `tokens`; ни там ни там → `skipped/unknown_product` (поведение не ослаблено).
- Ветка `KIND_SUBSCRIPTION`: пустой оверлей → `CLOUDPAYMENTS_PRODUCT_TOKENS` / фиксированный грант; оверлей → его `tokens`.
- Две ветки проверяются **раздельно**: резолвер отвечает на «сколько», классификация по `payment_type` и реклассификация [ADR-057](../../adr/ADR-057-cloudpayments-payment-type-mismatch-fallback.md) — на «какого класса платёж», и одно не подменяет другое.
- Идемпотентность по `payment_id` не меняется при активном оверлее.
- Архивный продукт: оплата по нему **начисляет**, из `GET /v1/tokens/products` он исчез.
