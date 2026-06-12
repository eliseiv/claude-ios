# billing-adapty / 02 — API Contracts

## POST /v1/billing/adapty/webhook

Серверный вебхук Adapty. **Вызывает Adapty**, не iOS-клиент. Контракт целиком в [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md).

### Авторизация
- `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` — статический секрет, заданный оператором в Adapty UI.
- НЕ пользовательский JWT, НЕ `X-Admin-Token`. Отдельный контур (третий тип авторизации; добавить в [API-REFERENCE §2](../../API-REFERENCE.md)).
- Сравнение constant-time (`hmac.compare_digest`). Неверный/нет токена → `401`. Секрет не сконфигурирован (`ADAPTY_WEBHOOK_SECRET` пуст) → `500`.

### Тело запроса
- **Без схемы / без Pydantic-валидации.** Читается сырое (`await request.body()`). Adapty при сохранении вебхука шлёт проверочный пинг с пустым/не-JSON/неполным телом — он обязан получить `2xx`.
- Ожидаемая (best-effort) форма распознаваемого события:
```json
{
  "event_id": "<unique>",
  "event_type": "subscription_started|subscription_renewed|subscription_cancelled|subscription_expired",
  "customer_user_id": "<наш userId UUID>",
  "event_properties": {
    "vendor_product_id": "<product>",
    "expires_at": "2026-07-12T00:00:00Z"
  },
  "profile": { "customer_user_id": "...", "expires_at": "..." }
}
```
Альтернативные имена полей (по версиям Adapty) — см. дефенсивный парсинг в [03-architecture.md](03-architecture.md).

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
| 200 | `{"result":"ignored","reason":"missing_customer_user_id"}` | нет/не-UUID `customer_user_id` |
| 200 | `{"result":"ignored","reason":"user_not_found"}` | пользователь не найден |
| 200 | `{"result":"ignored","event_type":"<echo>"}` | неизвестный `event_type` |
| 200 | `{"result":"duplicate"}` | повтор `event_id` |
| 200 | `{"result":"applied"}` | событие применено |
| 500 | (внутренний сбой) | БД недоступна и т. п. → Adapty ретраит |

### Эффекты при `applied`
- `subscription_started/renewed`: `subscriptions.status=active`, `plan=vendor_product_id`, `expires_at` (если есть); грант кредитов по тиру (идемпотентно `adapty-event:{event_id}`).
- `subscription_cancelled/expired`: `subscriptions.status=expired`; кредиты не изменяются.

### Идемпотентность
Повтор `event_id` → `duplicate` без побочных эффектов (UNIQUE `adapty_webhook_events.event_id` + UNIQUE ledger `idempotency_key`). Подробности — [ADR-029 §6](../../adr/ADR-029-adapty-subscription-webhook.md).
