# billing-cloudpayments / 02 — API Contracts

Модуль содержит **две половины** RU-контура:
- **Исходящая** — `POST /v1/billing/cloudpayments/checkout` ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)): **наш** JWT-эндпоинт создаёт платёжную ссылку через broadapps.
- **Входящая** — `POST /v1/billing/cloudpayments/webhook` ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)): broadapps присылает колбэк о состоявшейся оплате.

## POST /v1/billing/cloudpayments/checkout

**Наш** эндпоинт создания платёжной ссылки RU-оплаты. **Вызывает iOS-клиент** (JWT). Делает один исходящий вызов broadapps `POST /payments/link` и возвращает `paymentUrl` (ссылка YooKassa). Контракт целиком — [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md). Активен **только на avelyra** (где заданы `CLOUDPAYMENTS_APP_ID`+`CLOUDPAYMENTS_API_TOKEN`).

### Авторизация
- Пользовательский **JWT** (`Authorization: Bearer <JWT>`, `bearerAuth`, `CurrentUser`) — как прочие `/v1/*`. Нет/невалидный → `401`.
- **`userId` берётся из JWT `sub`, НЕ из тела** — ключевая мера (устраняет клиент-контролируемый `user_id`, из-за которого платежи «терялись»). Тело **не содержит** `userId`/`appId`.
- Rate-limit `enforce_other_limits(user_id=sub)` → `429`.

### Тело запроса (`CloudPaymentsCheckoutRequest`, StrictModel `extra="forbid"`)

```json
{ "productId": "week_6.99_nottrial", "customerEmail": "user@example.com" }
```

| Поле | Тип | Обязательно | Правило |
|---|---|---|---|
| `productId` | str | да | непустой; валидируется allowlist-предикатом `classify_product` (см. ниже). Неизвестный/некредитуемый → `422` |
| `customerEmail` | EmailStr | да | email покупателя (broadapps требует). Невалидный → `422` |

**Валидация `productId`** (симметрия с вебхуком, [03-architecture §Валидация productId](03-architecture.md)): `classify_product(productId, billing_interval_unit=None, frozenset(token_products()))`; `unknown` → `422`; `tokens` с `token_products().get(productId,0)<=0` → `422`. `productId` НЕ определяет сумму гранта (только allowlist-гейт).

### Ответ (`CloudPaymentsCheckoutResponse`, StrictModel) — проброс полей broadapps

```json
{ "paymentId": "e3d7ffe4-...", "paymentUrl": "https://yoomoney.ru/checkout/payments/v2/contract?orderId=...", "status": "pending", "expiresAt": null }
```

| Поле | Тип | Источник (broadapps) |
|---|---|---|
| `paymentId` | str | `payment_id` |
| `paymentUrl` | str | `payment_url` (ссылка YooKassa; тип `str`, не `HttpUrl` — passthrough) |
| `status` | str | `status` |
| `expiresAt` | str \| null | `expires_at` (nullable, passthrough без парсинга) |

### Коды ответа

| HTTP | Код | Когда |
|---|---|---|
| 200 | — | успех: broadapps вернул ссылку (`result=created`) |
| 401 | `unauthorized` | нет/невалидный JWT |
| 422 | `validation_error` | неизвестный/некредитуемый `productId`, невалидный `customerEmail`, лишнее поле |
| 429 | `rate_limited` | превышен лимит |
| 502 | `upstream_error` | broadapps недоступен/таймаут/не-2xx/malformed-ответ (без утечки деталей/токена) |
| 503 | `cloudpayments_checkout_not_configured` | `CLOUDPAYMENTS_APP_ID`/`CLOUDPAYMENTS_API_TOKEN` не заданы (⇒ активен только на avelyra) |

### Исходящий вызов broadapps
- `POST {CLOUDPAYMENTS_API_BASE}/payments/link` (default base `https://pay.broadapps.dev/api/v1`), **multipart/form-data** {`app_id`(config), `product_id`, `user_id`(=JWT `sub`), `customer_email`}, `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>`, `Accept: application/json`, таймаут 15с. Детали — [03-architecture §Исходящий вызов](03-architecture.md).

> **Контракт исходящего вызова — сверить живьём ([Q-051-1](../../99-open-questions.md)):** имена multipart-полей и shape `201`-ответа взяты из спеки заказчика; после деплоя прислать тестовый checkout и убедиться, что broadapps вернул `payment_url`.

---

## POST /v1/billing/cloudpayments/webhook

Серверный вебхук агрегатора **broadapps** в формате **CloudPayments**. **Вызывает broadapps**, не iOS-клиент. Контракт целиком в [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md).

### Авторизация
- Статический секрет `CLOUDPAYMENTS_WEBHOOK_TOKEN` (на avelyra = app API key broadapps) в заголовке `Authorization`.
- НЕ пользовательский JWT, НЕ `X-Admin-Token`, НЕ Adapty-секрет. Отдельный (пятый) machine-to-machine контур.
- **Приём заголовка ТЕРПИМ к формату ([ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)):** broadapps — партнёрский отправитель с нефиксированным форматом. Принимаются все формы (извлекается один и тот же credential, сравнивается constant-time с секретом):
  - `Authorization: Bearer <token>` (регистронезависимо к слову `Bearer`);
  - `Authorization: Token <token>`;
  - `Authorization: <token>` — «сырой», без схемы.
  - Нераспознанная схема (`Basic …` и т. п.) → весь заголовок сравнивается как есть → не совпадёт → `401` (fail-closed).
- Сравнение constant-time (`hmac.compare_digest`); «нет заголовка» и «неверный токен» → одинаковый `401` (без раскрытия причины), оба всегда проходят compare (нет timing-leak). Секрет не сконфигурирован (`CLOUDPAYMENTS_WEBHOOK_TOKEN` пуст) → `500` (⇒ эндпоинт активен только там, где секрет задан). Токен/полный заголовок **не логируются**.
- **Диагностика 401 ([ADR-052 §3](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md), [08-observability](08-observability.md)):** на каждый `401` — один WARNING-лог `"cloudpayments_webhook_auth_denied"` c безопасным allowlist (`matched`, слово-схема `authScheme`, ИМЕНА присутствующих auth-заголовков) — чтобы увидеть, если broadapps шлёт секрет в другом заголовке/как подпись. Значения/секрет не логируются.
- **OpenAPI:** security-схема `cloudPaymentsWebhook` (`HTTPBearer`) сохраняется декоративно (замок/Authorize в Swagger), реальная проверка — из сырого заголовка.

### Тело запроса
- **Без схемы / без Pydantic-валидации.** Читается сырое (`await request.body()`). Кривой payload не должен давать `422` (иначе агрегатор может ретраить).
- Плоские поля — **PascalCase**; поля внутри `Data` — **snake_case**. **`Data` — это JSON-строка** (парсится отдельным `json.loads`; дефенсивно принимается и уже-словарь).

#### Ключевые поля (пример — годовая подписка)
```json
{
  "Data": "{\"user_id\":\"B284721F-C3E0-4446-B00F-3C6A21F32535\",\"product_id\":\"yearly_49.99_nottrial\",\"billing_interval_unit\":\"year\",\"billing_interval_count\":\"1\",\"billing_phase\":\"regular\",\"subscription_id\":\"f95b318c-...\",\"is_trial_initial\":false,\"paymentGateway\":\"yookassa\"}",
  "Status": "Completed", "OperationType": "Payment", "TestMode": false,
  "Amount": 3990, "Currency": "RUB",
  "AccountId": "B284721F-C3E0-4446-B00F-3C6A21F32535",
  "TransactionId": "31d884c8-000f-5001-8000-1fb75b44e1d9",
  "SubscriptionId": "f95b318c-...",
  "CardFirstSix": "220024", "CardLastFour": "8808", "Issuer": "VTB", "CardType": "Mir", "Description": "Годовая подписка"
}
```
- `AccountId` (верх) → fallback `Data.user_id` — **идентификатор-кандидат `X`** (приходит в ВЕРХНЕМ регистре → нормализуется к lower, парсится в UUID). На RU-флоу broadapps шлёт сюда **deviceId** (id устройства), а не наш `userId`; резолв `X`→наш `userId` — **двухступенчатый** ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md), см. §Резолв пользователя ниже).
- `TransactionId` (верх) — ключ дедупа события и идемпотентности гранта.
- `CardFirstSix`/`CardLastFour`/`Issuer`/`CardType` — **PII, НЕ логируются и НЕ персистятся** ([08-observability](08-observability.md)).

Полный порядок источников/парсинг — [03-architecture.md §Дефенсивный парсинг](03-architecture.md).

#### Резолв пользователя — двухступенчатый (deviceId → userId, [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md))
Идентификатор `X` (из `AccountId`→`Data.user_id`, lower/UUID) резолвится в наш `userId` в **две ступени** (первое совпадение выигрывает), детали — [03-architecture.md §Резолв пользователя](03-architecture.md#резолв-пользователя--двухступенчатый-deviceid--userid-adr-053):

| Ступень | Условие | Результат | `resolvedVia` (лог) |
|---|---|---|---|
| (a) | `X` есть в `users` | `userId = X` (уже наш id; обратная совместимость с [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)) | `user_id` |
| (b) | иначе `X` есть в `auth_devices.device_id` | `userId = auth_devices[X].user_id` (deviceId→userId, [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)) | `device_id` |
| (c) | иначе (нет ни в `users`, ни в `auth_devices`) | `ignored/user_not_found` — `200 {"code":0}` + WARNING, **без** создания пользователя/устройства | — |

**Всё начисление/подписка/дедуп/идемпотентность/audit — на резолвнутый `userId`** (не на deviceId). Идемпотентность `cp-txn:{TransactionId}` и anti-tamper не меняются. Маппинг deviceId→userId — **только из нашей `auth_devices`** (телу колбэка не доверяем). Скоуп — только этот вебхук (Adapty/checkout не затронуты).

#### Гейт и классификация продукта
- Обрабатывается только `Status=="Completed"` (ci) И `OperationType=="Payment"` (ci); иначе `ignored/not_a_completed_payment`.
- `classify_product` → `subscription` | `tokens` | `unknown` (правила — [03-architecture.md §Классификация](03-architecture.md)).

#### Маппинг (ADR-050)

| Класс | Эффект `subscriptions` | Кредиты |
|---|---|---|
| `subscription` (интервал в `Data` или паттерн имени) | upsert `active`, `plan=product_id`, `expires_at=now+interval` | грант `cloudpayments_product_tokens().get(product_id) or cloudpayments_subscription_tokens_grant`, идемпотентно по `cp-txn:{TransactionId}` |
| `tokens` (в `TOKEN_PRODUCTS` или паттерн `NNN_tokens`) | не трогается | разовый грант `N = token_products().get(product_id)`; не в карте → `unknown_product` |
| `unknown` | — | `200 {"code":0}` (WARNING) |

### Ответы

Все `200` c телом `{"code": 0}`, кроме `401`/`500`. Внутренний исход (`result`/`reason`) уходит **в лог/audit**, а не в тело (CloudPayments ждёт только `{"code":0}`).

| HTTP | Тело | Когда (лог `result/reason`) |
|---|---|---|
| 401 | (ошибка авторизации) | нет/неверный bearer |
| 500 | (ошибка мис-конфигурации) | `CLOUDPAYMENTS_WEBHOOK_TOKEN` не задан |
| 200 | `{"code":0}` | `ignored/empty_body` (пустое тело) |
| 200 | `{"code":0}` | `ignored/invalid_json` (не-JSON верхний уровень) |
| 200 | `{"code":0}` | `ignored/not_an_object` (JSON не объект) |
| 200 | `{"code":0}` | `ignored/not_a_completed_payment` (Status≠Completed или OperationType≠Payment) |
| 200 | `{"code":0}` | `ignored/missing_transaction_id` |
| 200 | `{"code":0}` | `ignored/invalid_data` (`Data` нет/не парсится) |
| 200 | `{"code":0}` | `ignored/missing_product_id` |
| 200 | `{"code":0}` | `ignored/invalid_account_id` (нет/не-UUID `AccountId`/`Data.user_id`) |
| 200 | `{"code":0}` | `ignored/user_not_found` (WARNING) — `X` не найден **ни** в `users`, **ни** в `auth_devices.device_id` ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)); без провижининга |
| 200 | `{"code":0}` | `ignored/unknown_product` (WARNING) |
| 200 | `{"code":0}` | `duplicate` (повтор `TransactionId`) |
| 200 | `{"code":0}` | `applied` (subscription/tokens начислено) |
| 500 | (внутренний сбой) | БД недоступна и т. п. → агрегатор ретраит |

> **Формат ответа — ДОПУЩЕНИЕ ([Q-050-1](../../99-open-questions.md)):** apidog broadapps запаролена. Принято `{"code":0}` на всё обработанное (CloudPayments-стандарт success). Нужны ли reject-коды (`11` invalid AccountId и т.п.) — [Q-050-2](../../99-open-questions.md), **проверить живьём** после деплоя.

### Эффекты при `applied`
- **subscription:** `subscriptions.status=active`, `plan=product_id`, `expires_at=now+interval(billing_interval_unit × billing_interval_count)`; грант кредитов per-tier, идемпотентно по `cp-txn:{TransactionId}`.
- **tokens:** разовый грант `N` из `TOKEN_PRODUCTS`; подписка не трогается; идемпотентно по `cp-txn:{TransactionId}`.

### Идемпотентность (разведены два механизма — образец [ADR-047 §C](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md))
- **Дедуп события:** повтор `TransactionId` → `duplicate` без побочных эффектов (UNIQUE/PK `cloudpayments_webhook_events.transaction_id`).
- **Идемпотентность начисления:** ledger `idempotency_key = cp-txn:{TransactionId}` (UNIQUE, [ADR-005](../../adr/ADR-005-idempotency-ledger.md)); namespace изолирован от `adapty-txn:*`/`sub-grant:*`/`admin-sub-grant:*`/token-purchase. Один грант на платёж; продление приходит новым `TransactionId` → новый грант.
