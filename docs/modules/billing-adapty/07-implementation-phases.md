# billing-adapty / 07 — Implementation Phases

Реализация [ADR-029](../../adr/ADR-029-adapty-subscription-webhook.md). Backend выполняет фазы строго по порядку.

## Фаза 1 — Config + миграция
- `src/app/config.py`: добавить
  - `adapty_webhook_secret: str = Field(default="", alias="ADAPTY_WEBHOOK_SECRET")`
  - `adapty_product_tokens_raw: str = Field(default="{}", alias="ADAPTY_PRODUCT_TOKENS")`
  - `adapty_subscription_tokens_grant: int = Field(default=1000, alias="ADAPTY_SUBSCRIPTION_TOKENS_GRANT")`
  - метод `adapty_product_tokens() -> dict[str, int]` по образцу `token_products()` (`config.py:199`): JSON `{str: positive-int}`, малформед → `{}`, bool исключить.
- Миграция **`0008`** (после `0007`): таблица `adapty_webhook_events` (DDL — [04-data-model.md](04-data-model.md)) + index по `user_id`. ORM-модель `AdaptyWebhookEvent` в `src/app/models/tables.py`.
- Audit: `EVENT_ADAPTY_SUBSCRIPTION = "adapty_subscription"` в `src/app/audit/service.py`.

## Фаза 2 — Авторизация
- `require_adapty_webhook` (constant-time bearer, образец `auth.py:99-134`): извлечь токен после `Bearer `, `compare_digest` с `settings.adapty_webhook_secret`; пустой секрет → `500`; mismatch/нет → `401`.
- OpenAPI security-схема (http bearer, `auto_error=False`), образец `admin_scheme` (`openapi_security.py`).

## Фаза 3 — Парсинг + сервис
- Дефенсивный парсинг полей (точные источники — [03-architecture.md](03-architecture.md)): `event_id`, `event_type→lower`, `customer_user_id→UUID`, `vendor_product_id`, `expires_at→ISO8601`.
- `AdaptyWebhookService.handle(raw: bytes)`: матрица `ignored/*` (пустое/не-JSON/не-объект/missing/user_not_found) до транзакции.
- Тир: `adapty_product_tokens().get(vendor_product_id) or adapty_subscription_tokens_grant`.

## Фаза 4 — Транзакция
- Одна транзакция: `INSERT adapty_webhook_events ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id` → пусто ⇒ `duplicate`; иначе upsert `subscriptions` (active|expired) + (для started/renewed) `WalletService.grant(idempotency_key="adapty-event:{event_id}", reason="adapty_subscription", meta={...})` + audit. Commit. Сбой → ROLLBACK → 500.

## Фаза 5 — Router + регистрация
- `POST /v1/billing/adapty/webhook`: сырое тело (`await request.body()`), без Pydantic body-модели; per-route bearer Depends; матрица ответов (точные коды — [02-api-contracts.md](02-api-contracts.md)).
- Регистрация роутера в `src/app/main.py` (`include_router`, рядом со строками 196-212).
- `SizeLimitMiddleware` — стандартный лимит тела достаточен (payload подписки невелик); повышенный лимит роута НЕ требуется.

## Фаза 6 — Deployment (devops, после backend)
- Завести env `ADAPTY_WEBHOOK_SECRET` (per-instance, secret manager), `ADAPTY_PRODUCT_TOKENS`, `ADAPTY_SUBSCRIPTION_TOKENS_GRANT` — см. [07-deployment.md](../../07-deployment.md). Внести в prod-checklist (high-entropy секрет, разный на инстанс).

## Тестовые ориентиры (для qa)
- 401 на нет/неверный bearer; 500 на незаданный секрет.
- 200 `ignored/*` на каждый невалидный случай (включая проверочный пинг — пустое тело).
- `applied` для started/renewed (+ грант, + subscription active); `applied` для cancelled/expired (status expired, баланс не изменился).
- `duplicate` на повтор `event_id` (баланс не изменился — двойная UNIQUE-граница).
- Тир: vendor_product_id из карты → точное число; вне карты → fallback grant.
- Дефенсивный парсинг альтернативных имён полей (`id`, `profile.customer_user_id`, `product_id`).
