# Admin — Architecture

## Размещение
- Новый пакет `src/app/admin/` (router + thin service-обёртка над Wallet) и роутер `api_gateway/routers/admin.py`
  под префиксом `/v1/admin`. Структура — в [02-tech-stack.md](../../02-tech-stack.md#структура-проекта-фактическая).
- Admin-роуты подключаются с зависимостью `require_admin` и **без** `get_current_user`.

## Авторизация: `require_admin` (ADR-009)
```
require_admin(x_admin_token: str = Header(...)) -> None  # actor="admin", без userId
```
- Сравнивает `X-Admin-Token` с `ADMIN_API_SECRET` (и опц. `ADMIN_API_SECRET_PREV` на время ротации) — **constant-time**
  (`hmac.compare_digest`). Несовпадение/отсутствие → `401`.
- **НЕ** выполняет lazy-provisioning, **НЕ** читает/трогает `users.trial_used`, **НЕ** создаёт строку `users` для actor.
- Никакого `sub`/пользовательской идентичности: actor фиксируется как `admin` в audit.
- Изоляция: разные секреты/заголовки/зависимости с пользовательским путём → эскалация невозможна by construction (ADR-009 §4).

## Поток grant
```mermaid
sequenceDiagram
    participant OP as Operator
    participant GW as API Gateway (/v1/admin)
    participant ADM as Admin Service
    participant W as Wallet
    participant AU as Audit

    OP->>GW: POST /v1/admin/wallet/grant (X-Admin-Token, {userId, amount, idempotencyKey, reason})
    GW->>GW: require_admin (constant-time compare; rate limit; size limit; validate extra=forbid)
    alt токен невалиден
        GW-->>OP: 401
    else токен валиден
        GW->>ADM: grant(userId, amount, idempotencyKey, reason)
        ADM->>ADM: проверка существования users(userId)
        alt userId не существует
            ADM-->>OP: 404 user_not_found
        else существует
            ADM->>W: WalletService.grant(user_id, amount, idempotency_key, meta{reason}, reason)
            W->>AU: audit billing_credit (idempotent)
            W-->>ADM: {newBalance, ledgerTxId, idempotentReplay}
            ADM->>AU: audit admin_grant (actor=admin, reason, userId, amount, ledgerTxId; БЕЗ секрета)
            ADM-->>OP: 200 {newBalance, ledgerTxId, idempotentReplay}
        end
    end
```

## Поток subscription/grant (ADR-048)
Активация/продление подписки **без** StoreKit-транзакции — для саппорта/компенсации/теста ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md)).
Мотивация: по [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) при `subscription_status=none` кредиты не проверяются (блок `trial_used` при ненулевом балансе) — начисления кредитов мало, нужна активная подписка.
```mermaid
sequenceDiagram
    participant OP as Operator
    participant GW as API Gateway (/v1/admin)
    participant ADM as Admin Service
    participant DB as subscriptions
    participant W as Wallet
    participant AU as Audit

    OP->>GW: POST /v1/admin/subscription/grant (X-Admin-Token, {userId, expiresAt|days, plan?, idempotencyKey, credits?})
    GW->>GW: require_admin (constant-time); admin rate limit; body <= 8 KB; extra=forbid; ровно один из expiresAt/days; expiresAt > now()
    alt токен невалиден
        GW-->>OP: 401
    else токен валиден
        GW->>ADM: grant_subscription(userId, expires_at, plan, idempotencyKey, credits)
        ADM->>ADM: _require_user_exists(userId)
        alt userId не существует
            ADM-->>OP: 404 user_not_found
        else существует
            ADM->>DB: upsert subscriptions (status='active', plan, expires_at) — без StoreKit-verify
            opt эффективные credits > 0 (по умолчанию SUBSCRIPTION_CREDITS_PER_PERIOD)
                ADM->>W: WalletService.grant(user_id, credits, key="admin-sub-grant:{idempotencyKey}", meta{reason})
                W->>AU: audit billing_credit (idempotent)
            end
            ADM->>AU: audit admin_subscription_grant (actor=admin, plan, status, expiresAt, creditsGranted; БЕЗ секрета)
            ADM-->>OP: 200 {status, expiresAt, plan, creditsGranted, newBalance?, ledgerTxId?, idempotentReplay?}
        end
    end
```
- **Upsert напрямую в AdminService** (ORM `Subscription`, `self._session`), **не** через `SubscriptionService` (тот неразрывно связывает upsert с StoreKit-verify — единая ответственность verify→normalize→upsert→grant→audit). Небольшое дублирование трёх присваиваний — сознательный размен ради изоляции verify-less admin-пути; [Q-048-2](../../99-open-questions.md) (не блокер).
- **lazy-expiry учтён:** `expiresAt` требуется строго в будущем — иначе policy-loader (`_effective_subscription_status`) трактовал бы `active` с прошлой датой как `expired`, и грант не снял бы блок.
- **Дефолт `credits`** = `SUBSCRIPTION_CREDITS_PER_PERIOD` (не 0): подписка `active` + баланс 0 = блок `credits_empty`; дефолт-0 не дал бы рабочего доступа «одним запросом». Явный `0` = активировать без начисления.
- Всё в одной транзакции запроса; частичного применения нет.

## Рост admin-surface (ADR-048)
Теперь **две** мутирующие admin-операции (`wallet/grant`, `subscription/grant`) под одним общим `ADMIN_API_SECRET` без scope/least-privilege ([ADR-009](../../adr/ADR-009-admin-token-auth.md) §Consequences). Приемлемо при узком круге операторов; атрибуция/least-privilege — [Q-009-1](../../99-open-questions.md) при дальнейшем росте surface.

## Несуществующий userId — решение
Admin-grant **не создаёт** пользователей (обоснование — [02-api-contracts.md §Обоснование](02-api-contracts.md#обоснование-404-на-несуществующем-userid-не-admin-provisioning)).
Проверка существования `users(userId)` выполняется **до** вызова `WalletService.grant` (который сам делает `_ensure_wallet`,
но не `users`). Отсутствие → `404 user_not_found`. Это сохраняет инвариант ADR-007: единственный путь рождения
идентичности — доверенный issuer.

## Переиспользование Wallet
- `grant`: вызывается **как есть** (`src/app/wallet/service.py:174`); сигнатура `grant(user_id, amount, idempotency_key, meta, reason)`,
  идемпотентна по `(user_id, idempotency_key)`, пишет ledger credit + audit `billing_credit`. `meta` admin-grant включает
  `{"source": "admin", "reason": reason}` (без секретов).
- `get_wallet_view`: для `GET /v1/admin/wallet/{userId}`.
- Admin-модуль **не** дублирует биллинг-логику — только тонкая обёртка (auth + проверка userId + дополнительный audit `admin_grant`).

## Периодные расходы: `GET /v1/admin/costs/daily` ([ADR-092](../../adr/ADR-092-crm-daily-costs-endpoint.md))

Read-only агрегат для страницы CRM «Расход API». Контракт — [02-api-contracts §GET /v1/admin/costs/daily](02-api-contracts.md#get-v1admincostsdaily--периодная-разбивка-расходов-adr-092).

**Размещение.** Роут — `src/app/api_gateway/routers/crm_admin.py:186-216` (общий CRM-роутер под тем
же `require_admin`); агрегатор — отдельный модуль `src/app/admin/crm_costs.py`; тонкая обёртка
`CrmAdminService.daily_costs` (`src/app/admin/crm_service.py:853-874`); схемы —
`src/app/schemas/crm_admin.py:125-149`.

**Разбор границ у этого эндпоинта свой.** `_parse_date` (`crm_admin.py:69-84`) вызывается **только**
из `crm_daily_costs` (`:208-209`) и требует ровно `YYYY-MM-DD`: форму проверяет
`_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")` (`:66`) **до** `strptime`, который сам
ширину компонент не проверяет и принял бы `2026-8-1` ([ADR-092 §5](../../adr/ADR-092-crm-daily-costs-endpoint.md)).
`strptime` сохранён после регулярки — он ловит календарную невалидность (`2026-02-30`).
Остальные CRM-ручки (`/stats`, `/users`) разбирают свои границы **другой** функцией — `_parse_dt`
(ISO datetime, `:42-49`), и ужесточение формы её не коснулось.

**Два SQL, один аккумулятор.**

```
GET /v1/admin/costs/daily
      │  _parse_date × 2 → 400 на кривом формате/периоде (422 — конвейером FastAPI)
      ▼
CrmAdminService.daily_costs           ── страница режется В ПАМЯТИ, не LIMIT/OFFSET-ом
      ▼
daily_cost_items(session, date_from, date_to, recovered_media_usd)
      ├── _CHAT_DAILY_SQL   → сырые суммы счётчиков по (день, модель) из chat_steps
      │        └── _collect_chat  → provider_of_chat_model + chat_cost_usd_of_totals + chat_billed_tokens
      │                             (имени модели нет → клетка PROVIDER_UNKNOWN, только requests)
      ├── _MEDIA_DAILY_SQL  → строки media_jobs по (день, модель, кредиты, число ассетов)
      │        └── _collect_media → provider_cost_usd как есть + восстановление из кредитов (ADR-079 §2)
      ▼
  dict[(день, провайдер) → _Slot(requests, spend_usd, tokens)] → sorted() → items
```

**Прайс в SQL не переезжает.** SQL отдаёт **сырые суммы счётчиков**, цену применяет
`app.pricing.provider_prices` — единственный дом цен и правила «модель → вендор». Переписать
таблицу цен и правило `claude*` в SQL-выражение значило бы завести вторую копию, которая молча
разойдётся с первой (те же числа питают колонку «Себестоимость» и блок «Доход и провайдеры»,
[ADR-079](../../adr/ADR-079-crm-provider-cost-duration-payments.md)). Цена выбора — агрегация в БД
(десятки строк на выходе вместо сотен тысяч), а не выгрузка `usage` каждого шага в Python.

**Восстановление media-цены переиспользуется, а не копируется.** `daily_cost_items` принимает
`recovered_media_usd` **снаружи** — ту же функцию, что питает колонку «Себестоимость»
(`CrmAdminService._provider_cost`), чтобы правила [ADR-079 §2](../../adr/ADR-079-crm-provider-cost-duration-payments.md)
не размножились во вторую копию.

**`_Slot` кодирует `null ≠ 0` структурно.** `spend_usd` / `tokens` стартуют с `None` («ещё ничего не
измерено») и становятся числом только при первом успешном слагаемом; `requests` стартует с нуля —
число обращений известно всегда. Поэтому «не оценён ни один вызов» и «оценено на ноль» — разные
состояния **по построению**, а не по проверке в конце.

**Неатрибутируемый вызов заводит клетку, а не исчезает** ([ADR-092 §6](../../adr/ADR-092-crm-daily-costs-endpoint.md)).
Шаг с `usage`, но без строкового `model`, попадает в клетку `(день, PROVIDER_UNKNOWN)`: растёт
только `requests`, `spend_usd`/`tokens` остаются `None` (`src/app/admin/crm_costs.py:198-210`).
`PROVIDER_UNKNOWN = "Unknown"` (`src/app/pricing/provider_prices.py:129`) — **не вендор**:
`provider_of_chat_model` его никогда не возвращает. Отдельный ключ, а не вклад в `OpenAI`, потому
что вклад заразил бы `null`-ом или занизил без пометки уже верную клетку. Фильтр
`AND s.usage IS NOT NULL` (`:111`) при этом **сохранён**: шаг **без** `usage` — объявление
медиа-визарда, а не вызов LLM, и его счёт удвоил бы уже посчитанную строку `media_jobs`.

**Пагинация в памяти, а не в SQL.** Клеток не больше, чем длина периода × число сырых ключей
(≤ 92 × **4** — `OpenAI` / `Anthropic` / `Fal` / `Unknown`,
[ADR-092 §6](../../adr/ADR-092-crm-daily-costs-endpoint.md)) —
весь ответ на порядки меньше страницы контракта (`limit` до 1000). Отдельный
`COUNT(*)` ради `total` был бы вторым проходом по тем же данным ради числа, которое уже известно.

**Индекс.** Chat-половина отбирает шаги единственным предикатом по `created_at`, под который заведён
`ix_steps_created_at` (миграция `0029_steps_created_idx`, [ADR-092 §8](../../adr/ADR-092-crm-daily-costs-endpoint.md)).
Media-половина ведущего индекса не имеет — [TD-033](../../100-known-tech-debt.md).

**Наблюдаемость неоценимого вызова — на WRITE-path, не здесь.** `chat_unpriced_steps_total{model,reason}`
и лог `chat_step_unpriced` пишет момент создания шага (`report_chat_step_pricing`,
`src/app/pricing/provider_prices.py:239-255`), а не этот read-path: считая отсюда, серия считала бы
**рендеры** и молчала бы вовсе, пока оператор не откроет CRM — ровно в той слепой зоне, ради которой
она заведена. Реестр — [01-architecture.md §Observability](../../01-architecture.md#observability).

## Защита
- Отдельный rate limit на `/v1/admin/*` (per source IP, дефолт 10 req/min, env-конфиг). Изолирован от пользовательских лимитов.
- Size-лимит тела admin-grant ≤ 8 KB.
- `X-Admin-Token` добавлен в redaction allowlist (никогда не логируется; ADR-009 §6).
- strict Pydantic v2 (`extra='forbid'`), `amount > 0`, `reason` непустой.
