# billing-cloudpayments / 03 — Architecture

Реализует [ADR-050](../../adr/ADR-050-cloudpayments-webhook.md). Ниже — точные детали для backend (без додумывания). Образец структуры — модуль [billing-adapty](../billing-adapty/README.md).

## Файлы (целевая раскладка)
- `src/app/billing_cloudpayments/__init__.py`
- `src/app/api_gateway/routers/billing_cloudpayments.py` — эндпоинт `POST /v1/billing/cloudpayments/webhook` (`router`, prefix `/v1/billing/cloudpayments`, tag `Billing (CloudPayments)`), per-route bearer-dependency, сырое тело, делегирование в `CloudPaymentsWebhookService`, ответ `{"code": 0}`. (Роутеры проекта — в `api_gateway/routers/`, не в пакете модуля.)
- `src/app/billing_cloudpayments/auth.py` — `require_cloudpayments_webhook` (constant-time bearer) + `CloudPaymentsWebhookMisconfiguredError` (override `status_code = 500`, code `cloudpayments_webhook_misconfigured`). 401 при mismatch/нет токена, 500 при незаданном секрете.
- `src/app/billing_cloudpayments/parser.py` — чистые функции дефенсивного парсинга + `ParsedPayment` (dataclass), `classify_product`, санитизация payload, константы паттернов.
- `src/app/billing_cloudpayments/service.py` — `CloudPaymentsWebhookService` + `WebhookOutcome`: дедуп, классификация, транзакция (upsert subscription | грант tokens + audit), `_log_outcome`/`_level_for`.
- `src/app/schemas/billing_cloudpayments.py` — `CloudPaymentsWebhookResponse` (`{code: int}`, только для OpenAPI; тела запроса нет).
- OpenAPI-схема `cloudpayments_webhook_scheme` (`HTTPBearer`, `scheme_name="cloudPaymentsWebhook"`, `auto_error=False`) — в `src/app/api_gateway/openapi_security.py`.
- Фабрика `get_cloudpayments_webhook_service` — в `src/app/deps.py`.
- Модель `CloudPaymentsWebhookEvent` — в `src/app/models/tables.py`.
- Миграция `0014` — `migrations/versions/…_0014_cloudpayments_webhook_events.py` (`down_revision="0013"`).
- Config — `src/app/config.py` (3 новых поля + `cloudpayments_product_tokens()`).
- Audit — `EVENT_CLOUDPAYMENTS_PAYMENT = "cloudpayments_payment"` в `src/app/audit/service.py`.
- Регистрация роутера в `src/app/main.py` (`include_router`).

## Поток обработки

```mermaid
sequenceDiagram
    participant B as broadapps (CloudPayments fmt)
    participant R as Router (/v1/billing/cloudpayments/webhook)
    participant S as CloudPaymentsWebhookService
    participant DB as PostgreSQL (1 транзакция)
    B->>R: POST + Authorization: Bearer <token>
    R->>R: constant-time compare (401 / 500-if-unset)
    R->>R: raw = await request.body()  (без Pydantic)
    R->>S: handle(raw)
    S->>S: parse: empty/json/object → gate(Status,OperationType) → TransactionId → Data(json.loads) → product_id → AccountId(UUID,lower)
    alt любая невалидность / user_not_found / unknown_product
        S-->>R: outcome ignored/<reason>
    else валидный платёж
        S->>DB: BEGIN
        S->>DB: INSERT cloudpayments_webhook_events ON CONFLICT(transaction_id) DO NOTHING RETURNING
        alt конфликт (дубликат) — RETURNING пуст
            S-->>R: outcome duplicate
        else вставлено
            S->>DB: classify_product → subscription: upsert subscriptions(active) ; tokens: (без изм. subscriptions)
            S->>DB: WalletService.grant(idem="cp-txn:{TransactionId}")
            S->>DB: audit cloudpayments_payment (assert_no_secrets, санитизировано)
            S->>DB: COMMIT
            S-->>R: outcome applied
        end
    end
    R-->>B: HTTP 200 {"code": 0}   (или 401/500)
    Note over S,DB: любой сбой → ROLLBACK → 500 → агрегатор ретраит → чистая переобработка
```

## Авторизация (детали для backend)
- Заголовок `Authorization: Bearer <token>`. Извлечь токен (после `Bearer `), `hmac.compare_digest(token, settings.cloudpayments_webhook_token)`.
- `settings.cloudpayments_webhook_token == ""` → **500** (мис-конфигурация, текст `"cloudpayments webhook token not configured"`). Пустой секрет не матчит presented-токен.
- Mismatch / нет заголовка → **401** (`UnauthorizedError`, без раскрытия причины).
- Реализовать **per-route** (Depends), не глобальным middleware. Эндпоинт изолирован от JWT / admin / Adapty-контуров.
- OpenAPI: отдельная security-схема (http bearer, `auto_error=False`), образец `adapty_webhook_scheme`.

## Дефенсивный парсинг (точный порядок источников)

Хелпер `_first_str(*candidates)` возвращает первое непустое строковое значение (принимает `int`→`str(int)`; `None`/`""` пропускает). Плоский доступ — по top-level ключам PascalCase; `data = _parse_data(body)` — распарсенный объект `Data` (см. ниже).

- **`transaction_id`** = `_first_str(body["TransactionId"])`. Нет → `ignored/missing_transaction_id`. (Первичен top-level; в `Data` его нет.)
- **`status`** = `str(body.get("Status") or "").strip().lower()`; **`operation_type`** = `str(body.get("OperationType") or "").strip().lower()`. Гейт: `status == "completed" and operation_type == "payment"` иначе `ignored/not_a_completed_payment`.
- **`Data`-парсинг** (`_parse_data(body) -> dict | None`): `d = body.get("Data")`; если `isinstance(d, str)` → `json.loads(d)` (в try; ошибка → `None`); если `isinstance(d, dict)` → `d` (дефенсивно); иначе → `None`. `None`/не-объект → `ignored/invalid_data`.
- **`user_id`** = `_first_str(body["AccountId"], data["user_id"]).lower()` → `uuid.UUID(...)`. Нет/не-UUID → `ignored/invalid_account_id`. **Нормализация lower обязательна** (приходит в верхнем регистре).
- **`product_id`** = `_first_str(data["product_id"])`. Нет/пусто → `ignored/missing_product_id`.
- **`billing_interval_unit`** = `_first_str(data["billing_interval_unit"]).lower()` (может отсутствовать у token-пакета).
- **`billing_interval_count`** = `_parse_int(data["billing_interval_count"], default=1)` (приходит строкой `"1"`; `<1`/невалид → 1).
- **`billing_phase`** = `_first_str(data["billing_phase"])` (audit-only).
- **`subscription_id`** = `_first_str(data["subscription_id"], body["SubscriptionId"])` (audit-only).
- **`is_trial_initial`/`is_trial_conversion`/`is_initial_payment`** = строго `bool` из `data[...]` (иначе `None`; audit/лог-only).
- **`amount`** = `body.get("Amount")` / **`currency`** = `_first_str(body["Currency"])` / **`test_mode`** = строго bool `body.get("TestMode")` — только для санитизированного payload/audit.
- **PII (НЕ читать в бизнес-логику, НЕ хранить):** `CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`/`DateTime`/`Description` — исключены из `ParsedPayment` by-design.

`ParsedPayment` (dataclass): `transaction_id: str`, `user_id: uuid.UUID`, `product_id: str`, `status: str`, `operation_type: str`, `billing_interval_unit: str | None`, `billing_interval_count: int`, `billing_phase: str | None`, `subscription_id: str | None`, `is_trial_initial/is_trial_conversion/is_initial_payment: bool | None`, `amount: int | None`, `currency: str | None`, `test_mode: bool | None`, `kind: str` (заполняется `classify_product`).

> `status`/`operation_type` — нормализованные (lower-case) гейт-поля (`Status`/`OperationType`), сохраняются в `ParsedPayment`, т.к. санитизированный `payload`/audit ([04-data-model.md §Санитизированный payload](04-data-model.md)) содержит `status`/`operationType`. Карт-данные исключены by-design.

## Классификация продукта (`classify_product(product_id, billing_interval_unit, token_product_ids: frozenset[str]) -> str`)

`token_product_ids` передаётся **аргументом** (не резолвится внутри) — парсер остаётся набором чистых функций без импорта `settings`; ключи `settings.token_products()` резолвятся в сервисе и передаются как `frozenset`.

Детерминированный порядок (первое совпадение выигрывает):
1. `product_id in token_product_ids` (= ключи `settings.token_products()`) → `"tokens"`. (операторская карта consumable — приоритет)
2. `billing_interval_unit` непусто и `∈ {"year","month","week","day"}` → `"subscription"`. (интервал рекуррентности — сильнейший признак)
3. `re.match(r"^\d+_tokens", product_id, re.I)` → `"tokens"`. (паттерн имени; сумма — из `TOKEN_PRODUCTS`, §Грант tokens)
4. `_looks_like_subscription(product_id)` (содержит `week`/`month`/`year`/`day` **или** оканчивается на `_nottrial`/`_trial`, case-insensitive) → `"subscription"`.
5. иначе → `"unknown"` → `ignored/unknown_product` (**WARNING**).

Константы: `_TOKENS_NAME_RE = re.compile(r"^\d+_tokens", re.I)`, `_SUB_KEYWORDS = ("week","month","year","day")`, `_SUB_SUFFIXES = ("_nottrial","_trial")`, `_INTERVAL_UNITS = frozenset({"year","month","week","day"})`.

## Маппинг → эффект (`_apply`, после успешного дедуп-INSERT)

### subscription
- **upsert** `subscriptions` одним statement (образец [ADR-048](../../adr/ADR-048-admin-subscription-grant.md)):
  ```sql
  INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at)
  VALUES (:uid, 'active', :plan, :expires_at, now())
  ON CONFLICT (user_id) DO UPDATE SET
    status='active', plan=EXCLUDED.plan, expires_at=EXCLUDED.expires_at, updated_at=now()
  ```
  `plan = product_id`; `expires_at = _compute_expiry(now, unit, count)` (§Expiry).
- **Грант кредитов:** `credits = settings.cloudpayments_product_tokens().get(product_id) or settings.cloudpayments_subscription_tokens_grant`. `WalletService.grant(user_id, amount=credits, idempotency_key=f"cp-txn:{transaction_id}", reason="cloudpayments_subscription", meta={"transactionId": ..., "productId": ..., "kind": "subscription"})`.

### tokens (§Грант tokens)
- `credits = settings.token_products().get(product_id)`. **`None`/`<=0` → `ignored/unknown_product`** (WARNING; сумму НЕ угадывать из имени — anti-tamper [ADR-015](../../adr/ADR-015-consumable-token-iap.md) BR-TP-1). При этом дедуп-INSERT уже произошёл; поэтому классификацию `unknown_product` для token-паттерна проверять **до** записи мутаций — см. порядок в `_apply` ниже.
- **`subscriptions` НЕ трогается.** `WalletService.grant(..., idempotency_key=f"cp-txn:{transaction_id}", reason="cloudpayments_tokens", meta={..., "kind": "tokens"})`.

### Порядок в `_apply` (важно)
1. `classify_product` (чистая) — если `unknown` **и** это НЕ granting-путь ⇒ вернуть `ignored/unknown_product` **до** INSERT (без записи события).
2. Для `tokens`: если `token_products().get(product_id)` пусто ⇒ `ignored/unknown_product` **до** INSERT.
3. INSERT `cloudpayments_webhook_events ... ON CONFLICT DO NOTHING RETURNING`; пусто ⇒ `duplicate`.
4. subscription → upsert + grant; tokens → grant. audit. commit. → `applied`.

> Иными словами: событие записывается в журнал **только** когда оно будет реально применено (валидный, известный, начисляемый платёж) — как в billing-adapty (там `ignored` не пишет в журнал).

## Expiry (`_compute_expiry(now, unit, count) -> datetime`)
CloudPayments-тело **не** несёт явного срока. MVP-приближение по `timedelta`:
```
DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
expires_at = now + timedelta(days=DAYS.get(unit, 30) * count)
```
Неизвестный/None `unit` при классе subscription недостижим (класс subscription требует валидного unit ИЛИ имени — если по имени, `unit is None` → дефолт `month`/30д). Календарно-точный расчёт (relativedelta) — [Q-050-3](../../99-open-questions.md); допустимо, т.к. агрегатор пришлёт продление новым `TransactionId` к реальному сроку.

## Транзакционность
Вся обработка применяемого платежа — **одна** транзакция. INSERT `cloudpayments_webhook_events ... ON CONFLICT (transaction_id) DO NOTHING RETURNING transaction_id` — точка дедупа доставки события:
- `RETURNING` пуст → дубликат → `duplicate`, мутаций нет.
- иначе → upsert subscription (для subscription) / грант → audit → commit.
Сбой на любом шаге → ROLLBACK → 500 → агрегатор ретраит; на ретрае `transaction_id` свободен (INSERT откатился) ⇒ чистая переобработка; `grant` дополнительно идемпотентен по `cp-txn:{transaction_id}`.

## Audit
Новое `EVENT_CLOUDPAYMENTS_PAYMENT = "cloudpayments_payment"`. Пишется **только на `applied`** (внутри `_apply`, после upsert/grant). На `ignored`/`duplicate` — audit НЕ пишется.
```
AuditEvent(user_id=<uuid>, event_type=EVENT_CLOUDPAYMENTS_PAYMENT, payload={
    "transactionId": transaction_id, "productId": product_id, "kind": kind,
    "semantics": kind, "status": "active"|None, "plan": plan|None, "expiresAt": <iso|null>,
    "creditsGranted": credits, "billingPhase": billing_phase, "amount": amount, "currency": currency,
    "testMode": test_mode, "subscriptionId": subscription_id})
```
**НЕ класть** карт-данные (`CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`) и bearer. `assert_no_secrets` применяется в `AuditService.record`.

## Observability
Каждый вызов `handle()` — **ровно одна** запись `"cloudpayments_webhook_outcome"` (allowlist полей, уровни) — точное ТЗ в [08-observability.md](08-observability.md).
