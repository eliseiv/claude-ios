# billing-adapty / 03 — Architecture

Реализует [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md). Ниже — детали для backend.

## Файлы (фактическая раскладка)
- `src/app/billing_adapty/__init__.py`
- `src/app/api_gateway/routers/billing_adapty.py` — эндпоинт `POST /v1/billing/adapty/webhook` (`router`, prefix `/v1/billing/adapty`, tag `Billing (Adapty)`), per-route bearer-dependency, сырое тело, делегирование в `AdaptyWebhookService`. (Роутеры проекта живут в `api_gateway/routers/`, не в пакете модуля.)
- `src/app/billing_adapty/auth.py` — `require_adapty_webhook` (constant-time bearer) + `AdaptyWebhookMisconfiguredError` (override `status_code = 500`, code `adapty_webhook_misconfigured`). 401 при mismatch/нет токена, 500 при незаданном секрете.
- `src/app/billing_adapty/service.py` — `AdaptyWebhookService` + `WebhookOutcome`: дедуп, маппинг события, транзакция (upsert subscription + grant + audit).
- `src/app/billing_adapty/parser.py` — чистые функции дефенсивного парсинга полей + `ParsedEvent`, константы событий (`GRANTING_EVENTS`/`EXPIRING_EVENTS`/`KNOWN_EVENTS`).
- `src/app/schemas/billing_adapty.py` — `AdaptyWebhookResponse` (`{result, reason?, event_type?}`, только для OpenAPI; тела запроса нет).
- OpenAPI-схема `adapty_webhook_scheme` (`HTTPBearer`, `scheme_name="adaptyWebhook"`, `auto_error=False`) — в `src/app/api_gateway/openapi_security.py`.
- Фабрика `get_adapty_webhook_service` — в `src/app/deps.py`.
- Регистрация роутера в `src/app/main.py` (`include_router`).

## Поток обработки

```mermaid
sequenceDiagram
    participant A as Adapty
    participant R as Router (/v1/billing/adapty/webhook)
    participant S as AdaptyWebhookService
    participant DB as PostgreSQL (1 транзакция)
    A->>R: POST + Authorization: Bearer <secret>
    R->>R: constant-time compare (401 / 500-if-unset)
    R->>R: raw = await request.body()  (без Pydantic)
    R->>S: handle(raw)
    S->>S: parse: empty/json/object/event_id/customer_user_id
    alt любая невалидность
        S-->>R: 200 ignored/<reason>
    else валидное событие
        S->>DB: BEGIN
        S->>DB: INSERT adapty_webhook_events ON CONFLICT(event_id) DO NOTHING RETURNING event_id
        alt конфликт (дубликат) — RETURNING пуст
            Note over S,DB: мутаций нет (никакого audit/grant/upsert); транзакция фиксируется без изменений
            S-->>R: 200 duplicate
        else вставлено
            S->>DB: upsert subscriptions (active|expired)
            opt started|renewed
                S->>DB: WalletService.grant(idem="adapty-event:{event_id}")
            end
            S->>DB: audit adapty_subscription (assert_no_secrets)
            S->>DB: COMMIT
            S-->>R: 200 applied
        end
    end
    Note over S,DB: любой сбой → ROLLBACK → 500 → Adapty ретраит → чистая переобработка
```

## Авторизация (детали для backend)
- Заголовок `Authorization: Bearer <token>`. Извлечь токен (после `Bearer `), `hmac.compare_digest(token, settings.adapty_webhook_secret)`.
- `settings.adapty_webhook_secret == ""` → **500** (мис-конфигурация, текст вида `"adapty webhook secret not configured"`). Пустой секрет не матчит ни один presented-токен.
- Mismatch / нет заголовка → **401** (`UnauthorizedError`, без раскрытия причины).
- Реализовать **per-route** (Depends), не глобальным middleware. Эндпоинт изолирован от пользовательской JWT-цепочки и от admin-токена.
- OpenAPI: завести отдельную security-схему (http bearer) по образцу `admin_scheme` (`auto_error=False`), чтобы Swagger показал «Authorize» без дублирующего header-параметра.

## Матрица ответов (точные коды/статусы)
Все `200`, кроме явно `401`/`500`. Тело JSON `{result, reason?, event_type?}`.

| Условие | HTTP | `result` | `reason` |
|---|---|---|---|
| нет/неверный bearer | 401 | — | — |
| секрет не задан | 500 | — | — |
| пустое тело | 200 | `ignored` | `empty_body` |
| не-JSON | 200 | `ignored` | `invalid_json` |
| JSON не объект | 200 | `ignored` | `not_an_object` |
| нет `event_id` | 200 | `ignored` | `missing_event_id` |
| нет `customer_user_id` (или не UUID) | 200 | `ignored` | `missing_customer_user_id` |
| пользователь не найден | 200 | `ignored` | `user_not_found` |
| неизвестный `event_type` | 200 | `ignored` | (+ `event_type` эхо) |
| дубликат `event_id` | 200 | `duplicate` | — |
| валидное событие | 200 | `applied` | — |
| внутренний сбой (БД и т. п.) | 500 | — | — |

## Дефенсивный парсинг (порядок источников)
- `event_id = body.event_id or body.id`
- `event_type = (body.event_type or "").lower()`
- `customer_user_id = body.customer_user_id or body.profile.customer_user_id or body.user_id` → распарсить как UUID. Не-UUID → `ignored/missing_customer_user_id`.
- `vendor_product_id = body.event_properties.vendor_product_id or body.event_properties.product_id or body.vendor_product_id or body.product_id`
- `expires_at = body.event_properties.expires_at or body.profile.expires_at` (ISO8601 → tz-aware; нераспарсиваемое → `None`, событие всё равно обрабатывается)

Обращения к вложенным dict — безопасные (`isinstance(..., dict)`), отсутствующие — `None`.

## Маппинг событий
| `event_type` | `subscriptions.status` | `plan` | `expires_at` | grant |
|---|---|---|---|---|
| `subscription_started` | `active` | `vendor_product_id` | если есть | **да** (тир) |
| `subscription_renewed` | `active` | `vendor_product_id` | если есть | **да** (тир) |
| `subscription_cancelled` | `expired` | без изменения | без изменения | нет |
| `subscription_expired` | `expired` | без изменения | без изменения | нет |

## Транзакционность
Вся обработка распознанного события — **одна** транзакция (`async with session.begin()` или эквивалент). INSERT `adapty_webhook_events ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id` — единая точка дедупликации:
- `RETURNING` пуст → дубликат → `200 duplicate`, мутаций нет.
- иначе → upsert subscription → (опц.) `grant` → commit.
Сбой на любом шаге → ROLLBACK → 500 → Adapty ретраит. На ретрае `event_id` свободен (INSERT откатился) ⇒ чистая переобработка; `grant` дополнительно идемпотентен по `idempotency_key`.

## Тир product → tokens
```
tokens = settings.adapty_product_tokens().get(vendor_product_id) or settings.adapty_subscription_tokens_grant
```
`adapty_product_tokens()` — новый хелпер `Settings` по образцу `token_products()` (`config.py:199`): парсит `ADAPTY_PRODUCT_TOKENS` (JSON `{str: positive-int}`), малформед → `{}`. `adapty_subscription_tokens_grant` — int из `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` (дефолт 1000).

## Grant
```
WalletService.grant(
    user_id=<UUID customer_user_id>,
    amount=tokens,
    idempotency_key=f"adapty-event:{event_id}",
    reason="adapty_subscription",
    meta={"adaptyEventId": event_id, "eventType": event_type, "vendorProductId": vendor_product_id},
)
```

## Audit
Новое `EVENT_ADAPTY_SUBSCRIPTION = "adapty_subscription"` в `src/app/audit/service.py`.

**Audit пишется ТОЛЬКО на `applied`** (внутри `_apply`, после успешного INSERT-дедупа и upsert/grant). На `ignored` (любой `reason`, включая `user_not_found` и неизвестный `event_type`) и на `duplicate` — audit **НЕ пишется** (никаких мутаций). Это исключает шум аудита от проверочных пингов Adapty и от повторных доставок.

Запись (только на `applied`):
```
AuditEvent(user_id=<uuid>, event_type=EVENT_ADAPTY_SUBSCRIPTION, payload={
    "adaptyEventId": event_id, "eventType": event_type, "status": status,
    "plan": plan, "expiresAt": <iso|null>, "customerId": str(user_id)})
```
`assert_no_secrets` уже применяется внутри `AuditService.record`. Bearer-секрет в payload не кладём.
