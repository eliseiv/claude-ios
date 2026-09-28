# ADR-117 — `GET /v1/tokens/products`: period/interval → `kind: "subscription"`, без `credits` пакета

- **Статус:** Accepted. **Состояние реализации:** код не написан этим проходом (зона `backend`).
- **Дата:** 2026-09-28
- **Связано:** [ADR-015](ADR-015-consumable-token-iap.md) (каталог / purchase), [ADR-050](ADR-050-cloudpayments-webhook.md) (`classify_product`, interval units), [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) (класс по `payment_type` на начислении), [ADR-057](ADR-057-cloudpayments-payment-type-mismatch-fallback.md) (фолбэк при рассинхроне `payment_type`), [ADR-098](ADR-098-broadapps-paywall-experiments-and-default-product.md) (поля каталога), [ADR-099](ADR-099-crm-admin-economics-and-instance-settings.md) (оверлей витрины). Модуль [token-purchase](../modules/token-purchase/README.md), [billing-cloudpayments](../modules/billing-cloudpayments/README.md).
- **Характер:** уточнение маппинга живого рублёвого каталога broadapps → ответ `GET /v1/tokens/products`. Миграций нет. Начисление вебхука **не** пересматривается этим ADR (остаётся [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) + [ADR-057](ADR-057-cloudpayments-payment-type-mismatch-fallback.md)).

## Контекст

На RU-инстансах (в т.ч. elvarixa) broadapps отдаёт продукты вроде `monthly_19.99_nottrial` / `yearly_59.99_nottrial` с `payment_type: "one_time"` и непустым `subscription_interval_unit` (`month` | `year` | …). Текущий маппер `_from_broadapps` (`src/app/api_gateway/routers/token_purchase.py`) ставит `kind="subscription"` **только** при `payment_type == "subscription"` → иначе `kind="tokens"` и подставляет `credits` из `TOKEN_PRODUCTS`. Клиент видит подписочные тарифы как пакеты токенов.

Тот же класс рассинхрона уже закрыт на **пути начисления** [ADR-057](ADR-057-cloudpayments-payment-type-mismatch-fallback.md): при `payment_type=one_time` и коде, который `classify_product` считает подпиской, класс восстанавливается. Checkout уже гейтит через `classify_product` ([ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) §2). **Витрина** осталась на голом `payment_type` — симметрия «что показываем / что checkout продаёт / что вебхук начисляет» нарушена на первом звене.

`classify_product` (`src/app/billing_cloudpayments/parser.py`) уже трактует непустой `billing_interval_unit ∈ {year,month,week,day}` как `subscription` (шаг 2 детерминированного порядка). Поле broadapps `subscription_interval_unit` — тот же смысл для каталога продуктов.

**Решение владельца (принято, пересмотру не подлежит):** в ответе `GET /v1/tokens/products` продукт с period/interval (месяц/год/неделя/день) обязан иметь `kind: "subscription"` и **не** нести `credits` пакета токенов (как у подписки).

## Решение

### §1. Правило `kind` для ветки живого каталога broadapps

В маппере записи broadapps → `TokenProduct` продукт считается **подпиской**, если выполняется **хотя бы одно**:

1. `payment_type == "subscription"` (как сегодня); **или**
2. `subscription_interval_unit` — строка из того же набора, что `_INTERVAL_UNITS` у `classify_product`: `{year, month, week, day}` (сравнение case-insensitive после `strip`; пустое / иное / отсутствие поля → условие 2 ложно).

Иначе — `kind: "tokens"` (пакет).

`period` ответа по-прежнему = нормализованный `subscription_interval_unit` (строка или `null`), независимо от `kind`: у подписки по правилу (2) `period` обязан быть непустым; у пакета — обычно `null`.

### §2. `credits` у подписки

Если по §1 продукт — подписка → `credits: null` **всегда**, даже если код есть в `TOKEN_PRODUCTS`. Карта `TOKEN_PRODUCTS` остаётся источником кредитов **только** для `kind: "tokens"`. Это совпадает с уже описанным контрактом витрины ([token-purchase/02-api-contracts](../modules/token-purchase/02-api-contracts.md), [API-REFERENCE](../API-REFERENCE.md#get-v1tokensproducts)): у подписок `credits` = `null`.

Оверлей [ADR-099](ADR-099-crm-admin-economics-and-instance-settings.md) уточняет `credits`/`title` уже перечисленных строк: для строки, ставшей подпиской по §1, уточнение `credits` **не** поднимает пакетное число наружу — подписка остаётся с `credits: null` (класс витрины важнее оверлея числа пакета).

### §3. Что не меняется

- Порядок источников каталога (broadapps → `PRODUCTS_CATALOG` → `TOKEN_PRODUCTS`).
- `POST /v1/tokens/purchase` (StoreKit consumable) и policy-guard [Q-015-1](../99-open-questions.md).
- Классификация и начисление вебхука ([ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) + фолбэк [ADR-057](ADR-057-cloudpayments-payment-type-mismatch-fallback.md)): этот ADR чинит **витрину**, не путь денег. Симметрия восстанавливается тем, что витрина и checkout читают одно и то же понятие «есть interval → subscription».
- Ветки 2–3 каталога (без broadapps): `kind` из оверлея / статики по прежним правилам.

### §4. Наблюдаемость (опционально, не блокер)

При срабатывании правила (2) при `payment_type != "subscription"` допустим один WARNING на ответ каталога с полями `productId`, `paymentType`, `intervalUnit` (без секретов) — зеркало `cloudpayments_payment_type_mismatch`, но для read-path витрины. Отсутствие лога не блокирует приёмку.

## Последствия

- Клиент RU-пейволла видит monthly/yearly как `kind: "subscription"` с `period` и без ложных `credits`.
- Продукт с `payment_type=one_time` **без** interval по-прежнему пакет токенов.
- Backend: правка `_from_broadapps` (+ тесты маппера / integration каталога). Вне `docs/` — список в отчёте architect.

## Альтернативы

- **Править только `.env` / панель broadapps, чтобы `payment_type` стал `subscription`** — отклонено владельцем как единственный фикс: рассинхрон уже повторялся ([ADR-057](ADR-057-cloudpayments-payment-type-mismatch-fallback.md)); витрина должна переживать тот же класс, что начисление.
- **Копировать тело `classify_product` целиком в маппер (включая эвристику имени)** — отклонено для витрины: достаточно явного interval (поле поставщика); эвристика имени остаётся на checkout/фолбэке вебхука, где нет interval в колбэке.
- **Менять ADR-054: класс начисления только по interval** — отклонено: anti-tamper и фолбэк ADR-057 уже закрывают деньги; scope волны — витрина.
