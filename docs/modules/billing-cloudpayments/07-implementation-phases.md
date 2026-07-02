# billing-cloudpayments / 07 — Implementation Phases (ТЗ backend)

Реализация [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md). Backend выполняет фазы строго по порядку. **НЕ писать код в этом документе — это ТЗ.** Точные правила парсинга/маппинга — [03-architecture.md](03-architecture.md).

## Фаза 1 — Config + миграция + ORM + audit
- `src/app/config.py` (рядом с Adapty-блоком, `config.py:138-153`):
  - `cloudpayments_webhook_token: str = Field(default="", alias="CLOUDPAYMENTS_WEBHOOK_TOKEN")`
  - `cloudpayments_product_tokens_raw: str = Field(default="{}", alias="CLOUDPAYMENTS_PRODUCT_TOKENS")`
  - `cloudpayments_subscription_tokens_grant: int = Field(default=1000, alias="CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT")`
  - метод `cloudpayments_product_tokens() -> dict[str, int]` — **точная копия формы** `token_products()`/`adapty_product_tokens()` (`config.py:293`): JSON `{str: positive-int}`, малформед/не-объект → `{}`, `bool` исключить, невалидные пары пропустить (graceful, не крашить процесс).
- Миграция **`0014`** (`migrations/versions/…_0014_cloudpayments_webhook_events.py`, `down_revision="0013"`): таблица `cloudpayments_webhook_events` + index по `user_id` (DDL — [04-data-model.md](04-data-model.md)). Проверить **single head** (`alembic heads`).
- ORM-модель `CloudPaymentsWebhookEvent` в `src/app/models/tables.py`.
- Audit: `EVENT_CLOUDPAYMENTS_PAYMENT = "cloudpayments_payment"` в `src/app/audit/service.py`.

## Фаза 2 — Авторизация
- `src/app/billing_cloudpayments/auth.py`: `require_cloudpayments_webhook` (constant-time bearer, образец `require_adapty_webhook`): извлечь токен после `Bearer `, `hmac.compare_digest` с `settings.cloudpayments_webhook_token`; пустой секрет → `500` (`CloudPaymentsWebhookMisconfiguredError`, `status_code=500`, code `cloudpayments_webhook_misconfigured`); mismatch/нет → `401` (`UnauthorizedError`).
- OpenAPI security-схема `cloudpayments_webhook_scheme` (http bearer, `scheme_name="cloudPaymentsWebhook"`, `auto_error=False`) в `src/app/api_gateway/openapi_security.py`, образец `adapty_webhook_scheme`.

## Фаза 3 — Парсер (`src/app/billing_cloudpayments/parser.py`, чистые функции)
Точные источники/порядок — [03-architecture.md §Дефенсивный парсинг](03-architecture.md). Реализовать:
- `_first_str(*candidates) -> str | None` (принимает `int`→`str`, пропускает `None`/`""`).
- `_parse_int(value, default) -> int` (строка `"1"`→1; `<1`/невалид → default).
- `_parse_data(body) -> dict | None` (`Data`: str→`json.loads` в try; dict→as-is; иначе None).
- `parse_transaction_id`, `parse_gate(status, operation_type) -> bool`, `parse_user_id` (**`.lower()` + UUID**), `parse_product_id`, `parse_billing_interval_unit/count`, `parse_billing_phase`, `parse_subscription_id`, `parse_trial_flags` (строго bool|None), `parse_amount/currency/test_mode`.
- `classify_product(product_id, billing_interval_unit, token_product_ids: frozenset[str]) -> "subscription"|"tokens"|"unknown"` (5 шагов, [03-architecture.md §Классификация](03-architecture.md)). `token_product_ids` передаётся аргументом (чистая функция — без импорта `settings`; сервис резолвит `settings.token_products()` и передаёт ключи как `frozenset`). Константы: `_TOKENS_NAME_RE`, `_SUB_KEYWORDS`, `_SUB_SUFFIXES`, `_INTERVAL_UNITS`.
- `sanitize_payload(parsed) -> dict` — allowlist-проекция для персиста/audit ([04-data-model.md §Санитизированный payload](04-data-model.md)); **карт-данные исключены by-design**.
- `ParsedPayment` dataclass (поля — [03-architecture.md](03-architecture.md)).
- `_compute_expiry(now, unit, count) -> datetime` (timedelta-days `{day:1,week:7,month:30,year:365}×count`; None-unit → 30д).

**НЕ** читать `CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`/`DateTime`/`Description` в `ParsedPayment`.

## Фаза 4 — Сервис (`src/app/billing_cloudpayments/service.py`)
- `WebhookOutcome` (dataclass: `result: str`, `reason: str | None`). `_ignored(reason)` helper.
- `CloudPaymentsWebhookService.handle(raw: bytes) -> WebhookOutcome`:
  1. `not raw` → `ignored/empty_body`.
  2. `json.loads(raw)` (в try) → неуспех → `ignored/invalid_json`; результат не dict → `ignored/not_an_object`.
  3. `transaction_id` нет → `ignored/missing_transaction_id`.
  4. гейт `Status/OperationType` не прошёл → `ignored/not_a_completed_payment`.
  5. `_parse_data` None → `ignored/invalid_data`.
  6. `product_id` нет → `ignored/missing_product_id`.
  7. `user_id` (AccountId→Data.user_id, lower→UUID) невалиден → `ignored/invalid_account_id`.
  8. `not await self._user_exists(user_id)` → `ignored/user_not_found` (**WARNING**).
  9. `kind = classify_product(...)`: `unknown` → `ignored/unknown_product` (**WARNING**).
  10. если `kind == "tokens"` и `token_products().get(product_id)` пусто/<=0 → `ignored/unknown_product` (**WARNING**) — **до** INSERT.
  11. иначе → `await self._apply(parsed)`.
- `_apply(parsed)` (одна транзакция): INSERT-дедуп `cloudpayments_webhook_events ON CONFLICT(transaction_id) DO NOTHING RETURNING` → пусто ⇒ `duplicate`; иначе:
  - `subscription`: `_upsert_subscription(active, plan=product_id, expires_at=_compute_expiry(...))` + `_grant(reason="cloudpayments_subscription")` + audit.
  - `tokens`: `_grant(reason="cloudpayments_tokens")` (без upsert subscription) + audit.
  - commit → `applied`.
- `_grant(parsed, credits, reason)`: `WalletService.grant(user_id, amount=credits, idempotency_key=f"cp-txn:{transaction_id}", reason=reason, meta={"transactionId":..., "productId":..., "kind":...})`.
  - subscription credits = `cloudpayments_product_tokens().get(product_id) or cloudpayments_subscription_tokens_grant`.
  - tokens credits = `token_products().get(product_id)` (гарантированно >0 после шага 10).
- `_upsert_subscription`: параметризованный `INSERT ... ON CONFLICT (user_id) DO UPDATE` (образец `admin/service.py::grant_subscription`).
- Логирование исхода — Фаза 6 ([08-observability.md](08-observability.md)).

## Фаза 5 — Router + регистрация
- `src/app/api_gateway/routers/billing_cloudpayments.py`: `POST /v1/billing/cloudpayments/webhook`; сырое тело (`await request.body()`), без Pydantic body-модели; per-route `Depends(require_cloudpayments_webhook)`; вызвать `service.handle(raw)`; **всегда вернуть `JSONResponse({"code": 0}, status_code=200)`** для любого `WebhookOutcome` (result применён только в лог/audit). `401`/`500` поднимаются из auth-dependency / всплывают как ошибки БД.
- Фабрика `get_cloudpayments_webhook_service` в `src/app/deps.py`.
- Регистрация роутера в `src/app/main.py` (`include_router`, рядом с billing-adapty).
- `SizeLimitMiddleware` — стандартный лимит тела достаточен (payload платежа невелик); повышенный лимит роута НЕ требуется.
- **Swagger-чистота** ([R2ter](../../08-api-documentation.md)): `summary` (≤~60, напр. «Приём платежа RU (webhook)»), лаконичный `description` (что делает, что возвращает `{"code":0}`), схема `CloudPaymentsWebhookResponse` `Field`-ы — **без** ADR/TD/Q и внутренних имён таблиц/namespace.

## Фаза 6 — Наблюдаемость
- Только `src/app/billing_cloudpayments/service.py` (роутер не трогать). Логгер `logging.getLogger(__name__)` + `log_event(...)` (образец `app.billing_adapty.service`).
- Каждая return-точка `handle()`/`_apply()` — через `_log_outcome(outcome, *, transaction_id, product_id, user_id, kind)`; ровно один лог `"cloudpayments_webhook_outcome"` на вызов. Уровни/allowlist — [08-observability.md](08-observability.md).

## Что НЕ трогать
- Adapty-webhook (`billing_adapty/*`), StoreKit `/v1/subscription/sync`, `/v1/tokens/purchase`, BYOK, LLM-абстракцию, policy-engine.
- Схему `subscriptions`/`ledger_transactions`/`wallets` (только чтение/upsert/grant существующими сервисами).
- Существующие миграции (только новая `0014`).
- Карт-данные — не читать, не логировать, не персистить.

## Фаза 7 — Deployment (devops, после backend)
- Env `CLOUDPAYMENTS_WEBHOOK_TOKEN` (per-instance, secret manager) — задать **только на avelyra** (= app API key broadapps). `CLOUDPAYMENTS_PRODUCT_TOKENS` (JSON product→tokens тиров подписки) + `CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT` (fallback). См. [07-deployment.md](../../07-deployment.md).
- **Операторский шаг:** в панели broadapps сменить Callback URL с `…/v1/billing/adapty/webhook` на `…/v1/billing/cloudpayments/webhook`.
- Миграцию `0014` применить (`docker compose run --rm migrate`) на всех инстансах (таблица создаётся везде; эндпоинт активен только там, где задан токен).
- **После деплоя (сверка живьём, [Q-050-1/2](../../99-open-questions.md)):** прислать тестовый платёж; убедиться, что broadapps принял `{"code":0}` и не ретраит; проверить логи `"cloudpayments_webhook_outcome"`.

## Тестовые ориентиры (для qa) — кратко, полное — [09-testing.md](09-testing.md)
- 401 нет/неверный bearer; 500 незаданный секрет.
- `{"code":0}` на каждый `ignored/*` (включая пустое тело, не-JSON, не-Completed, unknown_product).
- Реальный payload годовой подписки → `subscriptions active`, `plan=yearly_49.99_nottrial`, `expires_at≈+365д`, **один** ledger-грант `cp-txn:{TransactionId}`.
- token-пакет (`product_id ∈ TOKEN_PRODUCTS`) → разовый грант N, подписка не тронута.
- Дубликат `TransactionId` → `duplicate`, баланс не изменился.
- `AccountId` верхним регистром → нормализован к lower → найден пользователь.
- Карт-данные отсутствуют в `payload`/audit/логах.
