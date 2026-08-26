# Admin — API Contracts

Все admin-эндпоинты под префиксом `/v1/admin/*`. Авторизация — изолированный admin-секрет
([ADR-009](../../adr/ADR-009-admin-token-auth.md)), зависимость `require_admin`. **Пользовательский
JWT не авторизует admin-действия.** Отдельный rate limit (дефолт 10 req/min per source IP,
конфигурируемо), `extra='forbid'`, тело ≤ 8 KB.

**Заголовок и коды отказа (сверено с `src/app/api_gateway/auth.py:126-141`):** принимается
`X-Admin-Key` (заголовок CRM/broad-crm) **или** легаси `X-Admin-Token`; при наличии обоих
выигрывает `X-Admin-Key`. Ни одного заголовка → **`403`** (`{error.code:"forbidden"}`); заголовок
есть, значение не совпало → **`401`** (`{error.code:"unauthorized"}`); секрет на инстансе не
сконфигурирован → **`401`** (fail-closed, до чтения заголовков). Сравнение — constant-time,
поддержана ротация (`ADMIN_API_SECRET_PREV`).

## POST /v1/admin/wallet/grant
Начисление кредитов пользователю (саппорт/компенсация).

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Request
```json
{
  "userId": "uuid",
  "amount": 100,
  "idempotencyKey": "string",
  "reason": "string"
}
```
- `userId` — UUID существующего пользователя (см. Правила §Несуществующий userId).
- `amount` — целое **> 0** (BIGINT, целые кредиты, без дробей). `amount <= 0` → `422`.
- `idempotencyKey` — непустая строка, `max_length` 128. Ключ идемпотентности начисления (передаётся в `WalletService.grant(idempotency_key=...)`).
- `reason` — **обязателен**, непустая строка, `max_length` 512. Пишется в audit `admin_grant` и `ledger_transactions.meta`.

### Response (200)
```json
{
  "newBalance": 1100,
  "ledgerTxId": "uuid",
  "idempotentReplay": false
}
```
- `newBalance` — баланс после начисления.
- `ledgerTxId` — id `ledger_transactions` (`type=credit`).
- `idempotentReplay` — `true`, если ключ уже был использован с тем же payload (повторного начисления не было).

### Правила
- Переиспользует `WalletService.grant(user_id, amount, idempotency_key, meta, reason)` (`src/app/wallet/service.py:174`)
  — атомарно, идемпотентно по `(user_id, idempotency_key)`, пишет `ledger_transactions(type=credit)` + audit `billing_credit`.
- **Дополнительно** пишется audit-событие `admin_grant` (actor=admin, `userId`, `amount`, `reason`, `idempotencyKey`,
  `ledgerTxId`) — отдельно от `billing_credit`, фиксирует именно admin-инициацию. **Секрет `X-Admin-Token` в audit не пишется.**
- Идемпотентность: тот же `idempotencyKey` + тот же payload → тот же `ledgerTxId`, `idempotentReplay=true`, без повторного начисления.
- Тот же `idempotencyKey`, **другой** `amount` → `409` (конфликт, как в `WalletService.grant`), без начисления.
- **Несуществующий userId → `404 {error.code:"user_not_found"}`** (admin-grant **не создаёт** пользователей — см. 03-architecture; обоснование ниже).
- `reason` отсутствует/пустой → `422`.

## POST /v1/admin/subscription/grant
Ручная активация/продление подписки пользователю (саппорт/компенсация/тестирование) — **без** StoreKit-транзакции ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md)).
Зачем отдельно от `wallet/grant`: по [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) при `subscription_status=none` кредиты **не проверяются** — пользователь блокируется по `trial_used`
**даже с ненулевым балансом**, поэтому одного начисления кредитов недостаточно, нужна активная подписка.

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Request
```json
{
  "userId": "uuid",
  "expiresAt": "2026-12-31T23:59:59Z",
  "days": 30,
  "plan": "manual_grant",
  "idempotencyKey": "string",
  "credits": 1000
}
```
- `userId` — UUID существующего пользователя. Отсутствует → `404 user_not_found` (admin **не создаёт** пользователей, [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)).
- Срок — **ровно одно** из `expiresAt` / `days` (оба или ни одного → `422`):
  - `expiresAt` — ISO8601 datetime, **tz-aware** и **строго в будущем** (`> now()`). В прошлом/naive → `422`. Требование «в будущем» обязательно: policy-loader (`src/app/policy/loader.py`) применяет lazy-expiry — `active` c `expires_at <= now()` трактуется как `expired`, т.е. грант в прошлое не дал бы доступа.
  - `days` — положительный int (`> 0`); сервер вычисляет `expires_at = now() + days`. `≤ 0` → `422`.
- `plan` — опц. строка (`max_length` 128), метка плана. Дефолт `"manual_grant"`.
- `idempotencyKey` — **обязателен**, непустая строка (`max_length` 128). Ключ идемпотентности начисления кредитов.
- `credits` — опц. int `≥ 0`. **Опущено (null) → `SUBSCRIPTION_CREDITS_PER_PERIOD`** (тот же пакет, что даёт реальный период — активированная подписка сразу рабочая). Явный `0` → активировать подписку **без** начисления (у пользователя уже есть баланс). `< 0` → `422`.

### Response (200)
```json
{
  "status": "active",
  "expiresAt": "2026-12-31T23:59:59Z",
  "plan": "manual_grant",
  "creditsGranted": 1000,
  "newBalance": 1100,
  "ledgerTxId": "uuid",
  "idempotentReplay": false
}
```
- `status` — новый статус подписки (`"active"`).
- `expiresAt` — эффективный момент истечения (из `expiresAt` или `now()+days`), ISO8601 | `null`.
- `plan` — записанный план | `null`.
- `creditsGranted` — эффективно начисленная сумма (0, если не начислялось).
- `newBalance` / `ledgerTxId` / `idempotentReplay` — присутствуют (не `null`) **только** при `creditsGranted > 0`; иначе `null` (ledger-транзакции нет).

### Правила
- Upsert строки `subscriptions` (PK `user_id`): `status='active'`, `plan`, `expires_at`. Прямая запись через ORM `Subscription`, **без** StoreKit-верификации (в отличие от `/v1/subscription/sync`). Idempotent по PK: повтор перезаписывает те же значения.
- При эффективной сумме `> 0` — начисление через `WalletService.grant(...)` **как есть** (`src/app/wallet/service.py:174`): атомарно, идемпотентно по `(user_id, idempotency_key)`, ledger `credit` + audit `billing_credit`. Ledger-ключ **производный с namespace**: `admin-sub-grant:{idempotencyKey}` (не коллидирует с `admin/wallet/grant` и `sub-grant:{transaction_id}`).
- Тот же `idempotencyKey` c **другим** `credits` → `409` (из `WalletService.grant`), активации/начисления нет.
- **Дополнительно** пишется audit-событие `admin_subscription_grant` (actor=admin, `userId`, `plan`, `status`, `expiresAt`, `creditsGranted`, `idempotencyKey`, `ledgerTxId` при наличии). **Секрет `X-Admin-Token` не логируется/не в audit.**
- Всё (upsert + grant + оба audit) — в **одной** транзакции запроса.
- **Коды:** `200`; `401`; `404` (`user_not_found`); `409` (тот же `idempotencyKey`, другой `credits`); `422` (нет `userId` / оба|ни одного из `expiresAt`/`days` / `expiresAt` не tz-aware|в прошлом / `days ≤ 0` / `credits < 0` / схема); `429`; `5xx`.
- **OpenAPI-тексты** (`summary`, `description` роута, `Field(description=...)`, тег `Admin`) — по [08-api-documentation §R2ter](../../08-api-documentation.md#r2ter-лаконичность-user-facing-текстов-для-тестировщиков): лаконичные профессиональные формулировки для оператора, **без** ссылок `ADR-`/`Q-`/`TD-` и расшифровок-аббревиатур в скобках. Внутренняя мотивация (root-cause ADR-002, обоснование дефолтов, namespace-ключ) — только здесь и в [ADR-048](../../adr/ADR-048-admin-subscription-grant.md), **не** в OpenAPI-строках.

## GET /v1/admin/wallet/{userId}
Read-only просмотр кошелька для саппорта.

### Headers
- `X-Admin-Token: <ADMIN_API_SECRET>` (обязателен).

### Response (200)
```json
{
  "userId": "uuid",
  "balance": 1100,
  "lastTransactions": [
    { "id": "uuid", "type": "credit|debit", "amount": 100, "createdAt": "ISO8601", "meta": {} }
  ]
}
```
- Переиспользует `WalletService.get_wallet_view(user_id, last_n)` (дефолт `last_n=20`, по `created_at DESC`).
- `meta` — без секретов (usage/model/reason).

### Правила
- Несуществующий `userId` → `404 {error.code:"user_not_found"}` (read-only не создаёт пользователя).
- Только чтение; не мутирует состояние и не пишет мутирующий audit (логируется на уровне tool/request lifecycle).

## Обоснование «404 на несуществующем userId» (не admin-provisioning)
Admin-grant **не создаёт** пользователей. Причины:
- Источник истины идентичности — доверенный JWT issuer ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md));
  создание `users` из admin-API ввело бы второй, неаутентифицированный путь рождения идентичности и риск
  начисления на «фантомный» (опечатанный) `userId`.
- `404` делает опечатку в `userId` видимой оператору сразу, вместо молчаливого создания мусорного аккаунта с балансом.
- Реальные пользователи создаются лениво при первом аутентифицированном запросе (ADR-007); к моменту легитимного
  admin-grant пользователь, как правило, уже существует. Если нужно начислить «наперёд» — это отдельный продуктовый
  вопрос, не решается тихим созданием строки. См. [Q-009-2](../../99-open-questions.md) (не блокер; дефолт — `404`).

## GET /v1/admin/costs/daily — периодная разбивка расходов ([ADR-092](../../adr/ADR-092-crm-daily-costs-endpoint.md))

Read-only. Сколько бизнес заплатил AI-провайдерам за каждый день выбранного периода и в чей счёт —
источник данных страницы CRM «Расход API».

> ⚠️ **Контракт заморожен НЕ здесь.** Путь, имена query-параметров, имена полей ответа, порядок
> сортировки, предел периода и семантика отсутствия заданы CRM (`broad-crm`, расширение контракта
> бэков **v1.3**, её `ADR-084 §1`) и **из этого репозитория не меняются**: обе стороны пишутся
> разными командами в разных репозиториях, и расхождение в имени поля даёт у оператора **молча
> пустой экран**, а не ошибку. Всё, что решено здесь (чем наполняются величины, когда `null`, какие
> коды отказа), — [ADR-092](../../adr/ADR-092-crm-daily-costs-endpoint.md).

### Headers
- `X-Admin-Key: <ADMIN_API_SECRET>` (или легаси `X-Admin-Token`) — см. коды отказа в шапке документа.

### Query
| Параметр | Тип | Обяз. | Значение |
|---|---|---|---|
| `date_from` | `YYYY-MM-DD` | **да** | Левая граница периода, **UTC, включительно** |
| `date_to` | `YYYY-MM-DD` | **да** | Правая граница периода, **UTC, включительно** |
| `limit` | int `1…1000` | нет | Размер страницы, дефолт **1000** |
| `offset` | int `≥ 0` | нет | Смещение страницы, дефолт **0** |

Максимальная длина периода — **92 дня** (`date_to − date_from + 1`).

### Response (200)
```json
{
  "total": 2,
  "items": [
    { "date": "2026-08-24", "provider": "OpenAI", "spend_usd": 0.0341, "requests": 87, "tokens": 412900.0 },
    { "date": "2026-08-24", "provider": "Fal",    "spend_usd": 0.78,   "requests": 3,  "tokens": 0.0 }
  ]
}
```
- `total` — число клеток `(день, провайдер)` за **весь** период, а не размер страницы: обходчик CRM
  листает до `len(items) < limit` и сверяет полноту обхода именно по нему.
- `date` — `YYYY-MM-DD`, календарь **UTC**.
- `provider` — **сырой** ключ бэка (`"OpenAI"` / `"Anthropic"` / `"Fal"` / `"Unknown"`).
  Нормализацию (`openai`/`anthropic`/`fal`/`other`) делает **потребитель**, не мы: незнакомый ключ
  CRM сводит в `other` и **не теряет**. `"Unknown"` — не вендор, а клетка неатрибутируемого трафика
  (см. Правила).
- `spend_usd` — `number | null`, USD, закупочные цены [ADR-079 §1](../../adr/ADR-079-crm-provider-cost-duration-payments.md).
- `requests` — `int`, число **оплаченных обращений к провайдеру** (см. Правила).
- `tokens` — `number | null`, токены, посчитанные провайдером (см. Правила).
- **Порядок — `date ASC, provider ASC`.** Пара `(date, provider)` в ответе уникальна, поэтому
  порядок полный и постраничная нарезка стабильна без дополнительного tie-break.

### Правила

- **`requests` считает ВЫЗОВЫ, а не ходы.** Шаг чата с непустым `usage` = один оплаченный вызов LLM
  (tool-loop одного хода — несколько вызовов, каждый оплачен отдельно); одна строка `media_jobs` =
  одна оплаченная генерация. Ход (`message_step_id`) — единица истории пользователя и отвечает на
  другой вопрос («за что списали кредиты»), здесь **не применяется**.
  > **Следствие:** дневная сумма `spend_usd` этого эндпоинта **≥** суммы колонки «Себестоимость»
  > вкладки «Запросы» за тот же день. Прайс и правило «модель → вендор» общие, расходится
  > гранулярность отбрасывания неоценимого: там отбрасывается целый **ход**, здесь — отдельный
  > **вызов**. Утверждать «ровно те же деньги» **нельзя**.
- **`tokens` — по конвенции прайс-строки.** Учитывается флаг `cache_read_in_input`
  ([ADR-079 §1](../../adr/ADR-079-crm-provider-cost-duration-payments.md)): OpenAI включает
  кэшированный префикс в `inputTokens`, и наивная сумма `input + cache_read` посчитала бы кэш
  **дважды** и разошлась бы со `spend_usd` той же клетки.
- **`Fal` отдаёт `tokens = 0.0`, а не `null`.** Ноль здесь — **измерение**: fal берёт деньги за кадры
  и секунды, токенов не считает вовсе. `null` объявил бы величину неизмеряемой и навсегда пометил бы
  токенную сводку CRM неполной у любого бэка, где есть генерации.
- **`null` ≠ `0` и ≠ «нет строки» — три разных случая, подменять нельзя:**

  | Что видит CRM | Что это значит |
  |---|---|
  | `404` на пути | Бэк **не реализует** расширение v1.3 (здесь неприменимо — реализует) |
  | `200`, строки за `(день, провайдер)` **нет** | Расхода в этот день по этому провайдеру **не было** — измеренный ноль |
  | `200`, строка есть, поле = `null` | Величина **не измерена**: не оценён **ни один** вызов клетки |

  Нулевые строки за каждый день × провайдера **не отдаются**. Частично оценённая клетка отдаёт
  сумму оценённого — честное занижение (наследуется от [ADR-079 §5](../../adr/ADR-079-crm-provider-cost-duration-payments.md):
  подставить ноль за неизвестную цену — записать факт, которого нет). `requests` у существующей
  клетки `null` не бывает: число обращений известно всегда.
- **Неатрибутируемый вызов отдаётся клеткой, а не исчезает** ([ADR-092 §6](../../adr/ADR-092-crm-daily-costs-endpoint.md)):
  вызов с `usage`, но без строкового имени модели → клетка с сырым ключом **`"Unknown"`**,
  `requests = N`, `spend_usd = null`, `tokens = null` (`src/app/admin/crm_costs.py:198-210`).
  Молчание превратило бы день, весь трафик которого неатрибутируем, в `$0.00` — то есть в
  измеренный ноль там, где расход был. `"Unknown"` — легальный сырой ключ: потребитель, не
  узнавший его, сводит клетку в `other` и не теряет.
  Отдельно: assistant-шаг **без** `usage` в `requests` **не входит и входить не должен** — это
  шаг-объявление медиа-визарда, а не вызов LLM; счёт удвоил бы уже посчитанную строку `media_jobs`.
  Поэтому список сырых ключей — **четыре**: `"OpenAI"`, `"Anthropic"`, `"Fal"`, `"Unknown"`.

### Коды

| Код | Когда |
|---|---|
| `200` | успех (в том числе `items: []` — за период расхода не было) |
| `400` | `date_from`/`date_to` не `YYYY-MM-DD`; `date_from > date_to`; период длиннее **92** дней |
| `401` | заголовок передан, значение не совпало; либо admin-секрет на инстансе не сконфигурирован |
| `403` | ни `X-Admin-Key`, ни `X-Admin-Token` не переданы |
| `422` | отсутствует обязательный `date_from`/`date_to`; `limit`/`offset` вне диапазона или не число (штатный конвейер валидации FastAPI) |
| `429` | admin rate limit |

> **`YYYY-MM-DD` — строго 4/2/2 ASCII-цифры.** Запись без ведущих нулей (`2026-8-1`), дата-время
> (`2026-08-10T00:00:00Z`) и любой другой формат → `400`. Форма проверяется отдельной регуляркой
> **до** разбора: `datetime.strptime` ширину компонент не проверяет и `2026-8-1` принял бы молча
> ([ADR-092 §5](../../adr/ADR-092-crm-daily-costs-endpoint.md)). Календарно невалидная дата
> правильной формы (`2026-02-30`) — тоже `400`.

> **`404` на этом пути не возникает никогда — и это нормативно.** По контракту v1.3 `404` означает
> ровно одно: «расширение не реализовано». Отдать его в ответ на кривой параметр значило бы сообщить
> CRM, что эндпоинта нет, и она перестала бы опрашивать бэк вовсе (`daily_costs_supported = false`).
> Поэтому невалидный период — `400`, а не `404` и не `422`.
