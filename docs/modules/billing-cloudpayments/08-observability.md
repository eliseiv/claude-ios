# billing-cloudpayments / 08 — Observability (логирование исхода вебхука)

Реализует [ADR-050 §7](../../adr/ADR-050-cloudpayments-webhook.md). Образец — [billing-adapty/08-observability.md](../billing-adapty/08-observability.md) ([ADR-046](../../adr/ADR-046-adapty-webhook-outcome-logging.md)). Точное ТЗ для backend — без додумывания. HTTP-семантику (`{"code":0}`/401/500), начисление, дедуп и контракт **не меняет**.

## Цель
Каждый вызов `CloudPaymentsWebhookService.handle()` пишет **ровно одну** структурную запись `"cloudpayments_webhook_outcome"`, чтобы исход (в т.ч. `user_not_found`/`unknown_product`) был виден оператору. Тело HTTP-ответа несёт только `{"code":0}` — причина исхода живёт **в логе**, не в ответе.

## Файл для правки
Только `src/app/billing_cloudpayments/service.py`. **Роутер НЕ трогать** (он всегда возвращает `{"code":0}` и не видит `product_id`/`user_id`).

## Импорты и логгер
```python
import logging
from app.observability.logging import log_event
logger = logging.getLogger(__name__)   # == "app.billing_cloudpayments.service"
```

## Helper эмиссии
```python
def _log_outcome(
    self,
    outcome: WebhookOutcome,
    *,
    transaction_id: str | None = None,
    product_id: str | None = None,
    user_id: uuid.UUID | None = None,
    kind: str | None = None,
) -> WebhookOutcome:
    level = _level_for(outcome.result, outcome.reason)
    log_event(
        logger, level, "cloudpayments_webhook_outcome",
        result=outcome.result, reason=outcome.reason,
        transactionId=transaction_id, productId=product_id,
        userId=str(user_id) if user_id is not None else None, kind=kind,
    )
    return outcome
```
- `user_id: uuid.UUID` → **обязательно** `str(...)` (иначе `json.dumps` падает). `None`-поля `JsonFormatter` выкидывает из JSON (отсутствие = «не распарсено»).

## Функция уровня
```python
def _level_for(result: str, reason: str | None) -> int:
    if result in ("applied", "duplicate"):
        return logging.INFO
    # result == "ignored"
    if reason in ("user_not_found", "unknown_product"):
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    # invalid_json | not_an_object | not_a_completed_payment | missing_transaction_id
    # | invalid_data | missing_product_id | invalid_account_id
    return logging.INFO
```

| `result` | `reason` | Level |
|---|---|---|
| `applied` | `None` | INFO |
| `duplicate` | `None` | INFO |
| `ignored` | `user_not_found` | **WARNING** |
| `ignored` | `unknown_product` | **WARNING** |
| `ignored` | `empty_body` | DEBUG |
| `ignored` | прочие (`invalid_json`/`not_an_object`/`not_a_completed_payment`/`missing_transaction_id`/`invalid_data`/`missing_product_id`/`invalid_account_id`) | INFO |

## Точки вызова (каждый `return` → ровно один лог)
В `handle()` каждый ранний `return _ignored(...)` оборачивается в `_log_outcome(...)` с тем контекстом, что **уже распарсен** на этой точке (`transaction_id`/`product_id`/`user_id`/`kind` — по мере доступности; иначе `None`). Финальная `return await self._apply(parsed)` **остаётся как есть** — `_apply` логирует свои `duplicate`/`applied` сам (иначе двойная запись).

| Точка | Исход | известные поля |
|---|---|---|
| `not raw` | `ignored/empty_body` | — |
| `json.loads` упал / не dict | `ignored/invalid_json` \| `not_an_object` | — |
| нет `TransactionId` | `ignored/missing_transaction_id` | — |
| гейт не прошёл | `ignored/not_a_completed_payment` | `transaction_id` |
| `Data` не парсится | `ignored/invalid_data` | `transaction_id` |
| нет `product_id` | `ignored/missing_product_id` | `transaction_id` |
| невалидный `AccountId` | `ignored/invalid_account_id` | `transaction_id`, `product_id` |
| пользователь не найден | `ignored/user_not_found` | `transaction_id`, `product_id`, `user_id` |
| `unknown` / token не в карте | `ignored/unknown_product` | `transaction_id`, `product_id`, `user_id`, `kind` |
| `_apply` → `duplicate`/`applied` | — | логирует `_apply` (`transaction_id`/`product_id`/`user_id`/`kind`) |

## Инвариант «ровно один лог на вызов»
Каждая return-ветка проходит через `_log_outcome` один раз; `handle` НЕ логирует на пути `_apply`.

## Allowlist / запрет (PII, секреты)
**Логируется только:** `result`, `reason`, `transactionId`, `productId`, `userId` (наш UUID, `str`), `kind`.
**ЗАПРЕЩЕНО:** карт-данные (`CardFirstSix`/`CardLastFour`/`Issuer`/`CardType`), `Authorization`/bearer/`CLOUDPAYMENTS_WEBHOOK_TOKEN`, сырой `raw`/`Data`-строка, `amount`/`currency` в логе (они только в санитизированном `payload`/audit, не в outcome-логе). Канон — [ADR-050 §7](../../adr/ADR-050-cloudpayments-webhook.md), [05-security.md](../../05-security.md#логирование-безопасное).

## Тестовые ориентиры (для qa)
- На **каждый** исход — ровно одна запись `"cloudpayments_webhook_outcome"` с корректными `result`/`reason`/`level`.
- `user_not_found`, `unknown_product` → **WARNING**; `applied`/`duplicate`/технические `ignored` → INFO; `empty_body` → DEBUG.
- `transactionId`/`userId`/`productId` присутствуют там, где распарсены; отсутствуют (не `null`-ключ) на ранних reason'ах.
- `userId` — строка UUID; в записи **нет** карт-данных, bearer, сырого payload.
