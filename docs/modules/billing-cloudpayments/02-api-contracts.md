# billing-cloudpayments / 02 — API Contracts

## POST /v1/billing/cloudpayments/webhook

Серверный вебхук агрегатора **broadapps** в формате **CloudPayments**. **Вызывает broadapps**, не iOS-клиент. Контракт целиком в [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md).

### Авторизация
- `Authorization: Bearer <CLOUDPAYMENTS_WEBHOOK_TOKEN>` — статический секрет (на avelyra = app API key broadapps).
- НЕ пользовательский JWT, НЕ `X-Admin-Token`, НЕ Adapty-секрет. Отдельный (пятый) machine-to-machine контур.
- Сравнение constant-time (`hmac.compare_digest`). Неверный/нет токена → `401`. Секрет не сконфигурирован (`CLOUDPAYMENTS_WEBHOOK_TOKEN` пуст) → `500` (⇒ эндпоинт активен только там, где секрет задан).

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
- `userId` (наш backend UUID) ← `AccountId` (верх) → fallback `Data.user_id`. **Приходит в ВЕРХНЕМ регистре — нормализуется к lower.**
- `TransactionId` (верх) — ключ дедупа события и идемпотентности гранта.
- `CardFirstSix`/`CardLastFour`/`Issuer`/`CardType` — **PII, НЕ логируются и НЕ персистятся** ([08-observability](08-observability.md)).

Полный порядок источников/парсинг — [03-architecture.md §Дефенсивный парсинг](03-architecture.md).

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
| 200 | `{"code":0}` | `ignored/user_not_found` (WARNING) |
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
