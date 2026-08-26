# Admin — Testing

## Unit
- `require_admin`: валидный `X-Admin-Token` → проходит; неверный/отсутствует → `401`; сравнение constant-time (по контракту,
  не таймингом). Совпадение с `ADMIN_API_SECRET_PREV` (ротация) → проходит. Пустые секреты в конфиге не матчатся.
- Pydantic-схема grant: `amount<=0` → `422`; пустой/отсутствующий `reason` → `422`; лишнее поле (`extra='forbid'`) → `422`.

## Integration (реальный PostgreSQL)
- `grant` на существующем `userId` → `ledger_transactions(type=credit)`, баланс += amount, `idempotentReplay=false`.
- Повторный `grant` тот же `idempotencyKey` + payload → `idempotentReplay=true`, баланс не меняется, тот же `ledgerTxId`.
- Тот же `idempotencyKey`, другой `amount` → `409`, без начисления.
- Несуществующий `userId` → `404 user_not_found`, строка `users` **не** создана, баланс не появился.
- `require_admin` не создаёт `users` для actor (нет provisioning): после серии admin-запросов нет «admin»-строки в `users`.
- `users.trial_used` не изменяется admin-операциями.
- audit: успешный `grant` создаёт **и** `billing_credit` (Wallet), **и** `admin_grant` (Admin). Секрет в payload отсутствует.
- `GET /v1/admin/wallet/{userId}`: корректные `balance` + `lastTransactions` (DESC); несуществующий → `404`.

## Integration — subscription/grant ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md))
- `POST /v1/admin/subscription/grant` на существующем `userId` (нет строки `subscriptions`) → **создаёт** строку `status='active'`, `plan`, `expires_at`; ответ `status='active'`, `expiresAt`, `plan`.
- Повторный вызов на существующей подписке (upsert) **идемпотентен по PK `user_id`**: перезаписывает те же значения, второй строки не появляется.
- `credits` **опущен** → начислено `SUBSCRIPTION_CREDITS_PER_PERIOD` (default); ответ `creditsGranted=SUBSCRIPTION_CREDITS_PER_PERIOD`, `newBalance`/`ledgerTxId`/`idempotentReplay` присутствуют.
- `credits=0` → активация **без** начисления: `ledger_transactions` не растёт, `creditsGranted=0`, `newBalance`/`ledgerTxId`/`idempotentReplay` = `null`.
- `credits=N` (N>0) → начислено ровно N; `creditsGranted=N`.
- **Namespace ledger-ключа:** начисление идёт по `admin-sub-grant:{idempotencyKey}` и **НЕ коллидирует** с `wallet/grant` (raw `idempotencyKey`), с `sub-grant:{transaction_id}` (реальный период) и `adapty-txn:{...}` (Adapty): один и тот же человекочитаемый `idempotencyKey`, использованный на `wallet/grant` и на `subscription/grant`, порождает **две разные** ledger-транзакции.
- Повтор `subscription/grant` с тем же `idempotencyKey` + тот же `credits` → `idempotentReplay=true`, баланс не меняется, тот же `ledgerTxId`.
- Тот же `idempotencyKey`, **другой** `credits` → `409` (из `WalletService.grant`); **ни** подписка **не** активирована повторно с иными полями сверх upsert, **ни** кредиты не начислены (одна транзакция откатывается целиком).
- Несуществующий `userId` → `404 user_not_found`, строка `users` **не** создана, `subscriptions` не появилась, баланс не появился (нет provisioning).
- **Одна транзакция (нет частичного применения):** при сбое начисления (`409`/insufficient) upsert подписки **не** коммитится — состояние `subscriptions`/`wallets`/`ledger` не меняется.

## Policy / E2E — root-cause ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md))
- Пользователь с `subscription_status=none` и `trial_used=true`, баланс `>0`: `/v1/chat/run` (или `/v1/policy/effective`) → **blocked** (`trial_used`) **до** гранта.
- После `POST /v1/admin/subscription/grant` (expiresAt в будущем, `credits` по умолчанию): тот же пользователь **проходит** policy (`allow`) — сняты `trial_used` и `credits_empty`; подписка `active` + баланс `>0`.
- **lazy-expiry:** грант с `expiresAt <= now()` был бы отклонён валидацией (`422`), поэтому кейс «active с прошлой датой → всё равно expired» не достижим через endpoint (регресс-защита `_effective_subscription_status`).

## Validation — subscription/grant
- `expiresAt` в прошлом → `422`; `expiresAt` без tzinfo (naive) → `422`.
- `days <= 0` → `422`.
- Заданы **оба** `expiresAt` и `days` → `422`; **ни одного** → `422`.
- `credits < 0` → `422`; лишнее поле (`extra='forbid'`) → `422`; отсутствует `userId`/`idempotencyKey` → `422`.

## Integration — `GET /v1/admin/costs/daily` ([ADR-092](../../adr/ADR-092-crm-daily-costs-endpoint.md))

> **Статус: покрыто** — `tests/integration/test_crm_daily_costs_adr092.py`, **32** кейса
> (21 тест-функция, две параметризованы на 7 и 6 входов); сверка 2026-08-26. Перечень ниже —
> одновременно ТЗ и карта покрытия.

- **Форма ответа и порядок:** два дня × два провайдера → `total` = 4, `items` отсортированы
  `date ASC, provider ASC`; `date` в формате `YYYY-MM-DD`; `provider` — **сырой** ключ (`"OpenAI"` /
  `"Anthropic"` / `"Fal"`), не нормализованный.
- **`requests` считает ВЫЗОВЫ, а не ходы:** один `message_step_id` с тремя assistant-шагами, у
  каждого непустой `usage` → `requests = 3` в клетке дня, **не** `1`.
- **Шаг без `usage` в счёт не входит:** ход медиа-визарда (assistant-шаг «Generation started …»
  **без** `usage` + строка `media_jobs`) → клетка `Fal` даёт `requests = 1`, а не `2`; клетки чата от
  этого шага не появляется вовсе (регресс-защита от двойного счёта генерации).
- **Кэш OpenAI не считается дважды:** шаг с `inputTokens = 1000`, `cacheReadTokens = 800` на модели
  с `cache_read_in_input` → `tokens` клетки посчитаны по конвенции прайса, и `spend_usd` той же
  клетки с ними согласован (не `input + cache_read`).
- **`Fal` отдаёт `tokens = 0.0`, а не `null`:** день только с media-генерациями → клетка `Fal` несёт
  `tokens = 0.0` и непустой `spend_usd`.
- **`null` только при нуле оценённых вызовов:** день, где **все** chat-шаги на модели без строки
  прайса → клетка есть, `requests > 0`, `spend_usd = null`, `tokens = null`.
- **Частично оценённая клетка занижает честно:** день из двух вызовов, один на известной модели,
  другой на неизвестной → `requests = 2`, `spend_usd` = стоимость **только** известного (не `null` и
  не сумма с нулём за неизвестный).
- **«Нет строки» ≠ нулевая строка:** день без трафика внутри периода → клетки за этот день в ответе
  **нет** (нулевых строк не отдаём).
- **Границы периода включительные, календарь UTC:** шаг ровно в `date_from 00:00:00Z` и шаг в
  `date_to 23:59:59Z` **входят**; шаг в `00:00:00Z` следующих за `date_to` суток — **нет**.
- **Пагинация:** `limit=1&offset=1` → `items` длиной 1 = вторая клетка глобального порядка, `total`
  = число клеток за **весь** период (не размер страницы).
- **Коды отказа:**
  - отсутствует `date_from` (или `date_to`) → **`422`**;
  - `limit=0` / `limit=1001` / `offset=-1` → **`422`**;
  - `date_from=2026-8-1` (не `YYYY-MM-DD`) → **`400`**;
  - `date_from > date_to` → **`400`**;
  - период 93 дня → **`400`**, ровно 92 дня → **`200`** (граница проверяется с обеих сторон);
  - **`404` не возвращается ни на одном из перечисленных входов** — регресс-защита: `404` на этом
    пути означает для CRM «расширение v1.3 не реализовано» и выключил бы опрос бэка целиком.
  - нет ни одного admin-заголовка → **`403`**; заголовок передан с неверным значением → **`401`**.
- **Клетка `Unknown` ([ADR-092 §6](../../adr/ADR-092-crm-daily-costs-endpoint.md)):** день, шаги
  которого несут `usage` **без** строкового `model` → клетка `provider = "Unknown"`, `requests = N`,
  `spend_usd = null`, `tokens = null`; клетки других провайдеров за тот же день остаются
  **неизменными** (неатрибутируемый вызов в них не подмешивается).
- **Конвенция Anthropic:** у модели **без** `cache_read_in_input` кэш-чтение к входу
  **прибавляется** (вход его не включает) — зеркало кейса OpenAI, чтобы конвенция не была
  «проверена» одной стороной.
- **Датированный снапшот прайса** оценивается по цене своего алиаса (побеждает длиннейший алиас), а
  не проваливается в «неизвестную модель».
- **Вызов без счётчиков токенов** попадает в `requests`, но не в `spend_usd`/`tokens`: обращение к
  провайдеру было, умножать на цену нечего.
- **Период в один день** = целые UTC-сутки от `00:00:00Z` до `23:59:59Z`.

## Unit — метрика неоценимого шага ([ADR-092 §7](../../adr/ADR-092-crm-daily-costs-endpoint.md))

> Покрыто (`tests/unit/test_chat_unpriced_metric_adr079.py`, `tests/integration/test_chat_unpriced_metric_adr079.py`,
> `tests/unit/test_chat_price_resolution_adr079.py`) — сверка 2026-08-26.

- `chat_unpriced_steps_total{model,reason}` растёт по одному на **LLM-вызов** с `reason ∈
  {unknown_model, no_model, no_token_counts}`; на оценимом шаге **молчит** (серия — сигнал отказа, а
  не счётчик трафика).
- Продюсер — **WRITE-path**: рендер CRM (`GET /v1/admin/users/{id}`, `/requests`, `/costs/daily`) серию
  **не** двигает.
- Лог `chat_step_unpriced` — **одна** строка на пару `(model, reason)` на процесс; за пределом
  `_LOGGED_UNPRICED_CAP` = 256 различных пар пишется **один** `chat_step_unpriced_log_capped`, после
  чего лог молчит (флуда WARNING нет).

## Security
- Пользовательский JWT на `/v1/admin/*` (без `X-Admin-Token`) → `401` (JWT не авторизует admin).
- `X-Admin-Token` на пользовательском роуте (`/v1/wallet`) не даёт доступа (там нужен JWT).
- `X-Admin-Token` не попадает в логи/audit (redaction).
- Rate limit `/v1/admin/*`: превышение дефолта → `429`, изолировано от пользовательских лимитов.
- Size-лимит admin-grant: тело > 8 KB → `413` (действует и на `subscription/grant`).
- audit: успешный `subscription/grant` создаёт `admin_subscription_grant` (actor=admin, `plan`/`status`/`expiresAt`/`creditsGranted`), при начислении — **и** `billing_credit`. Секрет `X-Admin-Token` в payload **отсутствует**.
