# Module: Billing — CloudPayments (broadapps / YooKassa, RU-путь)

- Статус: Спроектирован, ожидает реализации
- Ответственность: **RU-путь биллинга** (broadapps `pay.broadapps.dev`, фронтит YooKassa), независимый от Adapty и StoreKit. Две половины:
  - **Исходящая** — `POST /v1/billing/cloudpayments/checkout` ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)): наш JWT-эндпоинт создаёт платёжную ссылку через broadapps `/payments/link`; `userId` из JWT (не из тела) → фикс «потерянных платежей».
  - **Входящая** — `POST /v1/billing/cloudpayments/webhook` ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md)): колбэк broadapps в формате CloudPayments; идемпотентная активация подписки / начисление token-пакета и грант кредитов.

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [08-observability.md](08-observability.md) · [09-testing.md](09-testing.md)

## DoD — Checkout ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md))
- `POST /v1/billing/cloudpayments/checkout` — JWT (`CurrentUser`); нет/невалидный → `401`. **`userId` из JWT `sub`, НЕ из тела.**
- Тело (StrictModel): `productId` (валидируется `classify_product`-allowlist; unknown/некредитуемый → `422`), `customerEmail` (`EmailStr`; невалидный → `422`). Лишние поля → `422`.
- Исходящий httpx `POST {CLOUDPAYMENTS_API_BASE}/payments/link`, **multipart** {`app_id`,`product_id`,`user_id`(=sub),`customer_email`}, `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>`, таймаут 15с.
- Ошибки httpx/не-2xx/malformed → `502 upstream_error` (без утечки токена/деталей). Успех → `200` {`paymentId`,`paymentUrl`,`status`,`expiresAt`}.
- `CLOUDPAYMENTS_APP_ID`/`CLOUDPAYMENTS_API_TOKEN` не заданы → `503` (только avelyra). Rate-limit → `429`.
- **Без миграции.** `customer_email`/`CLOUDPAYMENTS_API_TOKEN`/`app_id` не логируются/не персистятся; лог `cloudpayments_checkout_outcome` (allowlist). Upstream фиксирован (нет SSRF).
- Swagger-чистота: user-facing строки без ADR/TD/Q.

## DoD — Webhook ([ADR-050](../../adr/ADR-050-cloudpayments-webhook.md))
- Эндпоинт авторизуется статическим секретом `CLOUDPAYMENTS_WEBHOOK_TOKEN` (constant-time, per-route); нет/неверный токен → `401`; секрет не задан → `500` (⇒ активен только на avelyra). **Приём заголовка `Authorization` терпим к формату** ([ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md)): `Bearer <token>` (ci) / `Token <token>` / сырой `<token>`; на 401 — WARNING `cloudpayments_webhook_auth_denied` (allowlist имён заголовков + слово-схема, без секрета).
- После авторизации любое тело (пустое/не-JSON/неполное/неизвестный продукт/дубликат/неизвестный пользователь) → **`200` c `{"code":0}`** (агрегатор не ретраит). `500` только при реальном сбое БД / незаданном секрете.
- **Резолв пользователя двухступенчатый** ([ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)): `X` ← `AccountId` (верх) → fallback `Data.user_id`; **нормализация к lower**; не-UUID → `ignored/invalid_account_id`. Резолв `X`→наш `userId`: (a) `X∈users`→`userId=X`; (b) иначе `X∈auth_devices.device_id`→связанный `user_id` (deviceId→userId; broadapps на RU-флоу шлёт deviceId); (c) иначе → `ignored/user_not_found` (без создания пользователя/устройства). Начисление/дедуп/идемпотентность — на резолвнутый `userId`; маппинг только из нашей `auth_devices`.
- Гейт: `Status=="Completed"` (ci) И `OperationType=="Payment"` (ci); иначе `ignored/not_a_completed_payment`.
- `Data` — JSON-**строка** (парсится отдельно); классификация продукта → subscription (upsert `active`+`plan`+`expires_at` + грант per-tier) ИЛИ tokens (разовый грант `N` из `TOKEN_PRODUCTS`) ИЛИ unknown (`ignored/unknown_product`, WARNING).
- Идемпотентность: дедуп события по `TransactionId` (UNIQUE `cloudpayments_webhook_events.transaction_id`); грант — один на платёж (ledger `cp-txn:{TransactionId}`, изолирован).
- **PII:** карт-данные (`CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`) и bearer **не логируются и не персистятся**; `payload` таблицы и лог/audit — только санитизированная проекция (allowlist).
- Audit `cloudpayments_payment` через `assert_no_secrets`.
- **Swagger-чистота** ([R2ter](../../08-api-documentation.md)): user-facing OpenAPI-строки без ADR/TD/Q и внутреннего жаргона.

## Границы (см. [00-overview.md](00-overview.md))
- **НЕ** трогает Adapty-webhook ([ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md)/[ADR-047](../../adr/ADR-047-adapty-real-payload-format-and-grant-idempotency.md)), StoreKit `/v1/subscription/sync`, `/v1/tokens/purchase`, BYOK, LLM-абстракцию, policy-engine.
- **НЕ** обрабатывает рефанды (агрегатор их не шлёт).
- **НЕ** создаёт пользователей.

## Changelog
- 2026-07-03: фикс резолва пользователя вебхука (architect), [ADR-053](../../adr/ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md). Корень инцидента «оплата есть — начисления нет» RU-флоу (прод avelyra, подтверждён БД + iOS `RU_PURCHASE_FLOW.md`): broadapps шлёт как `AccountId`/`Data.user_id` **deviceId** (напр. `55cbe083-...`), которого нет в `users`, → `user_not_found` → без начисления; связь deviceId→userId (`b0f407bd-...`) есть в `auth_devices` ([ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)). Фикс: **двухступенчатый резолв** в `service.py` (users → auth_devices → user_not_found); начисление на резолвнутый `userId`; наблюдаемость `resolvedVia`. Без миграции (`auth_devices` уже есть). Скоуп только CloudPayments-вебхук (Adapty/checkout не трогаются). Идемпотентность/anti-tamper без изменений. Файлы: `billing_cloudpayments/service.py`. Заведён [Q-053-1](../../99-open-questions.md); [Q-052-1](../../99-open-questions.md) остаётся открыт.
- 2026-07-03: фикс авторизации вебхука (architect), [ADR-052](../../adr/ADR-052-cloudpayments-webhook-lenient-auth-header.md). Инцидент прод avelyra: реальные колбэки broadapps → `401` при совпадающем значении секрета → проблема в **формате** заголовка. `require_cloudpayments_webhook` переходит на терпимый разбор сырого `Authorization` (`Bearer`/`Token`/сырой `<token>`), constant-time сохранён, + WARNING-лог `cloudpayments_webhook_auth_denied` на 401 (allowlist имён заголовков + слово-схема, без секрета). `HTTPBearer`-схема декоративна (Swagger-лок). Без миграции. Файлы: `billing_cloudpayments/auth.py`, `openapi_security.py` (description). Заведён [Q-052-1](../../99-open-questions.md).
- 2026-07-03: проектирование checkout (architect), [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md). Новый JWT-эндпоинт `POST /v1/billing/cloudpayments/checkout` — исходящий вызов broadapps `/payments/link` (multipart), `userId` из JWT (фикс «потерянных платежей»). 3 env `CLOUDPAYMENTS_API_BASE`/`CLOUDPAYMENTS_APP_ID`/`CLOUDPAYMENTS_API_TOKEN` (avelyra), зависимость `email-validator`. Без миграции. Заведён [Q-051-1](../../99-open-questions.md).
- 2026-07-02: проектирование (architect), [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md). Новый модуль/эндпоинт `POST /v1/billing/cloudpayments/webhook`, новая таблица `cloudpayments_webhook_events` (миграция `0014`, down_revision `0013`), env `CLOUDPAYMENTS_WEBHOOK_TOKEN` / `CLOUDPAYMENTS_PRODUCT_TOKENS` / `CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT`. Заведены [Q-050-1..4](../../99-open-questions.md). Активен только на avelyra.
