# ADR-055 — Резолв пользователя во входящем Adapty-вебхуке через `auth_devices` (customer_user_id → userId) + общий модуль резолва

- Статус: Accepted
- Дата: 2026-07-09
- Тип: bugfix-ADR, **исправляет [ADR-029](ADR-029-adapty-subscription-webhook.md) §3 / [ADR-047 §A](ADR-047-adapty-real-payload-format-and-grant-idempotency.md)** (резолв пользователя входящего Adapty-вебхука). **Портирует семантику [ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)** (тот же баг резолва, ранее починенный только в CloudPayments-ветке) в Adapty-ветку и **выносит резолв в общий модуль** для обоих вебхуков.
- Связано: [ADR-018](ADR-018-embedded-auth-issuer.md) (device-based identity, таблица `auth_devices`), [ADR-007](ADR-007-lazy-user-provisioning.md) (не провижинить пользователей из вебхука), [ADR-046](ADR-046-adapty-webhook-outcome-logging.md) (outcome-лог), [ADR-047](ADR-047-adapty-real-payload-format-and-grant-idempotency.md) (парсинг/маппинг/идемпотентность гранта), [ADR-005](ADR-005-idempotency-ledger.md) (идемпотентность гранта). Модули [billing-adapty](../modules/billing-adapty/README.md), [billing-cloudpayments](../modules/billing-cloudpayments/README.md).

## Context

**Инцидент (прод avelyra, подтверждён логами 7 дней + БД): Adapty-вебхук приходит, авторизуется (200), но события ВЫБРАСЫВАЮТСЯ — «оплата/подписка есть, начисления нет».**

На каждый успешно авторизованный `POST /v1/billing/adapty/webhook` (200 ×6 за окно) сервис пишет `WARNING adapty_webhook_outcome result:"ignored" reason:"user_not_found"` для `eventType ∈ {subscription_renewed, access_level_updated}`, `customerUserId:"35a95d9b-86bf-4d69-a5c8-8790e25fd9af"`.

Проверено в БД avelyra:
```
users[35a95d9b-...]        = ОТСУТСТВУЕТ       # это deviceId, НЕ userId
auth_devices[35a95d9b-...] = 894edaee-...      # deviceId → userId
users[894edaee-...]        = существует        # реальный пользователь
```

**Корень бага — тот же, что в [ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md), но в другом вебхуке.** У пользователя два идентификатора: **deviceId** (id устройства из auth-issuer, [ADR-018](ADR-018-embedded-auth-issuer.md)) и **JWT userId** (наш внутренний id в `users`). Adapty присылает в `customer_user_id` **deviceId** (`35a95d9b-...`), а `AdaptyWebhookService.handle()` Stage 3 (`_user_exists`, `src/app/billing_adapty/service.py:197`) ищет его **только в `users.id`** → не находит → `ignored/user_not_found` (200, чтобы Adapty не зациклил ретраи) → грант/подписка **не применяются**. Связь `deviceId → userId` есть в **нашей** таблице `auth_devices(device_id PK, user_id FK)`, но вебхук её не использует.

Причина, почему Adapty шлёт deviceId в `customer_user_id`, а не наш userId: iOS вызывает `Adapty.identify(<deviceId>)` идентификатором устройства (а не JWT `sub`) — симметрично RU-флоу, где клиент отправляет broadapps deviceId ([ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)). До [ADR-047](ADR-047-adapty-real-payload-format-and-grant-idempotency.md) считалось, что `customer_user_id` появится как наш userId после `Adapty.identify`; по факту он появился как **deviceId**, и вебхук на нём спотыкается на резолве.

**Ущерб (подтверждён).** У `894edaee` подписка `week_6.99_nottrial` истекла 08.07 08:02, хотя Adapty присылал `subscription_renewed` и 08.07, и 09.07 (оба выброшены как `user_not_found`). Промокоды тестировщиков не начисляются.

**Ключевое: этот же класс бага уже решён в CloudPayments-ветке ([ADR-053](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)) и НЕ перенесён сюда.** `src/app/billing_cloudpayments/service.py:233` содержит `_resolve_user(x) -> tuple[user_id, resolved_via] | None` (a: `users` → `resolved_via="user_id"`; b: `auth_devices.device_id` → `user_id`, `resolved_via="device_id"`; иначе None). Adapty-вебхук остался на одноступенчатом `_user_exists`. Дублирование логики резолва между двумя вебхуками — прямая причина, по которой фикс не распространился автоматически; третий вебхук повторил бы тот же баг.

## Decision

Две части: **(A)** вынести двухступенчатый резолв в общий модуль `app/billing_common/resolve.py` и переиспользовать в обоих вебхуках; **(B)** применить его в Adapty-вебхуке вместо `_user_exists`, начисляя на резолвнутый `userId`. Скоуп маппинга событий, идемпотентности, HTTP-семантики, контракта эндпоинта — **не меняется**.

### A. Общий модуль резолва `app/billing_common/resolve.py` (решение и обоснование)

**Решение: выносим в общий модуль, оба вебхука переиспользуют. Дублирование отвергнуто.**

Обоснование (с учётом связности модулей и текущей структуры):

- **Логика идентична и не имеет доменной связности с конкретным вебхуком.** Резолв — чистая DB-операция над `AsyncSession`: два `SELECT` (`users`, затем `auth_devices`), нормализованный вход-UUID, детерминированный first-match. Он не зависит ни от Adapty-парсера, ни от CloudPayments-верификации, ни от карт продуктов. Значит общий модуль — **лист** (leaf): оба billing-модуля зависят от `billing_common`, а `billing_common` — ни от кого. Циклов/повышения связности нет.
- **Дублирование = гарантированный дрейф и рецидив бага.** Инцидент буквально вызван тем, что фикс жил в одной копии. Третий платёжный контур (StoreKit reconcile, ещё один агрегатор) без общего модуля повторит `user_not_found`-баг. Единая точка резолва делает «правильный» путь единственным.
- **Стоимость выноса мала и локальна.** CloudPayments-`_resolve_user` уже имеет ровно нужную сигнатуру (`tuple[uuid.UUID, str] | None`) и семантику — вынос сводится к перемещению тела в свободную функцию и замене вызова; поведение байт-в-байт сохраняется.

**Контракт модуля (единственный источник истины резолва):**
```python
# src/app/billing_common/resolve.py
RESOLVED_VIA_USER_ID = "user_id"
RESOLVED_VIA_DEVICE_ID = "device_id"

async def resolve_user(
    session: AsyncSession, x: uuid.UUID
) -> tuple[uuid.UUID, str] | None:
    """deviceId/userId → наш userId. First-match wins, детерминированно.
    (a) X ∈ users.id            → (X, "user_id")
    (b) X ∈ auth_devices.device_id → (linked user_id, "device_id")
    (c) иначе                    → None
    """
```
- Порядок (a)→(b) детерминирован: приоритет трактовки «`X` — уже наш userId» (обратная совместимость; коллизия пространств deviceId/userId крайне маловероятна — клиентский UUID устройства vs серверный `gen_random_uuid()`, но порядок фиксирует однозначность).
- `auth_devices.device_id` — `TEXT PRIMARY KEY`, значения — lower-UUID-строки; поиск по `str(x)` уже-lower UUID. Маппинг берётся **только из нашей `auth_devices`** — телу вебхука для связи `X→userId` не доверяем.
- Не провижинит пользователей/устройства: `X` вне обеих таблиц → `None` (= `user_not_found`), [ADR-007](ADR-007-lazy-user-provisioning.md).

**Адаптация CloudPayments (behavior-preserving).** `CloudPaymentsWebhookService._resolve_user` удаляется; вызов `self._resolve_user(device_id)` заменяется на `resolve_user(self._session, device_id)`. Семантика/возврат/`resolvedVia`-значения идентичны ([ADR-053 §1](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)) — контракт CloudPayments не меняется, требуется лишь ре-верификация тестами.

### B. Резолв в Adapty-вебхуке (`billing_adapty/service.py`)

**Парсинг `customer_user_id` не меняется** ([ADR-047 §A](ADR-047-adapty-real-payload-format-and-grant-idempotency.md)): `parse_customer_user_id(body)` → `uuid.UUID | None`; отсутствует/не-UUID → `ignored/missing_customer_user_id` (**без изменений**). Меняется только Stage 3 — «существование пользователя» → «резолв пользователя»:

1. **Stage 3 (резолв вместо `_user_exists`).** Метод `_user_exists` удаляется. Вместо него:
   ```python
   resolved = await resolve_user(self._session, customer_user_id)
   if resolved is None:
       return self._log_outcome(_ignored("user_not_found"),
           event_type=event_type, event_id=event_id, customer_user_id=customer_user_id)
   resolved_user_id, resolved_via = resolved
   ```
   `reason:"user_not_found"` остаётся **только** когда `X` не найден НИ в `users`, НИ в `auth_devices` (§A c). Реальный прод-кейс (deviceId `35a95d9b` в `auth_devices`) теперь резолвится через (b) → `resolved_via="device_id"` → событие применяется.

2. **Всё начисление — на резолвнутый `userId`.** `resolved_user_id` (+`resolved_via`) прокидывается в `_apply(...)`. Внутри `_apply`/`_upsert_subscription`/`_read_subscription`/`_grant`/дедуп-INSERT/audit **везде** используется `resolved_user_id`, а НЕ `event.customer_user_id`:
   - дедуп `INSERT adapty_webhook_events (user_id = resolved_user_id, event_id, ...)`;
   - upsert `subscriptions WHERE user_id = resolved_user_id`;
   - `WalletService.grant(user_id=resolved_user_id, ...)`;
   - `AuditEvent(user_id=resolved_user_id, ...)`.
   `event.customer_user_id` остаётся **исходным** идентификатором Adapty (для логирования/трассировки), не мутируется. `ParsedEvent` не меняет форму.

3. **Идемпотентность гранта НЕ меняется** ([ADR-047 §C](ADR-047-adapty-real-payload-format-and-grant-idempotency.md)): ключ `adapty-txn:{transaction_id ‖ original_transaction_id ‖ event_id}` — по транзакции, НЕ по userId; namespace изолирован. Дедуп события — по `adapty_webhook_events.event_id` (=`profile_event_id`) — тоже не меняется. `classify_event` (GRANTING/EXPIRING/NOOP) и все эффекты — как есть.

### C. Наблюдаемость (outcome-лог, [ADR-046](ADR-046-adapty-webhook-outcome-logging.md))

`_log_outcome` получает два новых опциональных параметра; allowlist лога расширяется **только** внутренними-безопасными UUID/enum (карт-PII/секретов нет — их и не было):

- **`resolvedVia`** ∈ `"user_id"` | `"device_id"` — как в CloudPayments ([ADR-053 §3](ADR-053-cloudpayments-webhook-user-resolution-via-auth-devices.md)). Присутствует на исходах, где пользователь резолвнут (`applied`/`duplicate`/unknown-`event_type`); на `user_not_found`/ранних `ignored` — резолв не удался/не выполнялся → **опущено** (не `null`-ключ).
- **`resolvedUserId`** = резолвнутый наш внутренний UUID (`str`), присутствует там же, где `resolvedVia`. Даёт оператору увидеть реального получателя гранта, когда `customerUserId` = deviceId.
- **`customerUserId` — сохраняется как есть** (исходный `customer_user_id`, что прислал Adapty; параметр `_log_outcome` не меняет значение). Это наш внутренний id (deviceId либо userId) — безопасно логировать.

`_level_for` **не меняется** (уровни по `result`/`reason` те же: `user_not_found`/`missing_customer_user_id`/unknown-type → WARNING; `applied`/`duplicate` → INFO; и т.д.).

### D. Скоуп и что НЕ трогается

- **Без миграций** — `auth_devices` уже существует ([ADR-018](ADR-018-embedded-auth-issuer.md), миграция `0005`); новых колонок/таблиц нет.
- **Контракт эндпоинта не меняется**: путь `POST /v1/billing/adapty/webhook`, авторизация `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` (constant-time, 401/500-if-unset), коды ответов. Ответ Adapty остаётся **200 на `ignored`** (анти-ретрай).
- **`missing_customer_user_id`** — семантика без изменений (absent/не-UUID → `ignored`, WARNING).
- **Маппинг событий, `KNOWN_EVENTS`, `classify_event`, идемпотентность, дедуп, тир product→tokens, схема `AdaptyWebhookResponse`** — без изменений.
- **CloudPayments-контракт** — не меняется (только внутренний рефактор `_resolve_user` → общий `resolve_user`, поведение сохранено).

### E. Обратная совместимость

Если `customer_user_id` уже наш `userId` (есть в `users`) — путь (a), поведение **строго прежнее** (грант/подписка на него же, `resolvedVia="user_id"`). Новый путь (b) активируется только когда `X` не найден в `users`, но найден в `auth_devices` — раньше это давало `user_not_found`, теперь корректно начисляет. Чистое расширение резолва: ни один ранее успешный исход не меняется; часть ранее ошибочных `user_not_found` становится `applied`.

## Ретроактивные операторские действия (выполняет main chat / оператор, НЕ входит в backend-ТЗ)

Фикс лечит будущие вебхуки. Уже потерянные за инцидент состояния восстанавливаются вручную через admin-эндпоинт ([ADR-048](ADR-048-admin-subscription-grant.md), `POST /v1/admin/subscription/grant`, `X-Admin-Token`):

1. **avelyra:** продлить подписку `894edaee-0902-4cf6-82d4-c4ca13787cb4` (план `week_6.99_nottrial`, истекла 08.07 08:02) — восстановить активное окно до нового реального `subscription_expires_at` из Adapty; при необходимости начислить период кредитов (идемпотентный ключ, чтобы не задвоить с будущим вебхуком).
2. **orvianix:** разблокировать `2c10eec7-...` (аналогичный застрявший кейс) — admin-grant активной подписки/кредитов.

Эти операции — данные, не код; выполняются после деплоя фикса, чтобы будущие вебхуки того же пользователя резолвились через `device_id` и не задваивали (идемпотентность гранта по `transaction_id` защищает).

## Consequences

**Плюсы:**
- Закрыт корневой инцидент «подписка/оплата есть — начисления нет» Adapty-флоу: deviceId из `customer_user_id` резолвится в наш `userId` через `auth_devices`, подписка/кредиты начисляются реальному пользователю.
- Устранён источник рецидива: единый `resolve_user` для обоих вебхуков; третий контур не повторит баг.
- Обратная совместимость полная; маппинг из доверенной нашей БД, не из тела; без миграций, без изменения контракта/HTTP-семантики.
- Наблюдаемость `resolvedVia`/`resolvedUserId` даёт оператору явную картину пути резолва (ожидаемо `device_id` на текущем прод-флоу).

**Минусы / риски / долг:**
- Один доп. DB-lookup (`auth_devices`) на путь «не найден в `users`» (индексирован по PK; ничтожно).
- Рефактор живого CloudPayments-`_resolve_user` в общий модуль — behavior-preserving, но затрагивает прод-проверенный код; обязательна ре-верификация тестами (qa).
- Расхождение регистра deviceId между `/v1/auth/register` и `Adapty.identify` (если появится) → `user_not_found`; митигация — lower-нормализация + наблюдаемость (та же ось, что [Q-053-1](../99-open-questions.md)).
- **Открытый вопрос [Q-055-1](../99-open-questions.md):** `non_subscription_purchase` (консумируемые токены по промокоду) не входит в `KNOWN_EVENTS` — вне scope этого ADR (см. ниже), требует решения владельца.

## Alternatives (отвергнуто)

- **Дублировать резолв в Adapty (скопировать `_resolve_user`).** Отвергнуто: именно дублирование породило инцидент (фикс жил в одной копии); третий вебхук повторил бы баг. Общий модуль — единственный «правильный» путь.
- **Провижинить `users` из deviceId в вебхуке.** Отвергнуто: нарушает [ADR-007](ADR-007-lazy-user-provisioning.md); создаёт «пользователя-призрака» без JWT-истории, оторванного от реального аккаунта устройства.
- **Доверять маппингу из тела Adapty (клиент шлёт и deviceId, и userId).** Отвергнуто: клиент-контролируемый маппинг = подмена адресата гранта. Связь `deviceId→userId` — только из нашей `auth_devices`.
- **Чинить только на клиенте (iOS вызывает `Adapty.identify(<JWT userId>)`).** Отвергнуто как единственная мера: требует релиза iOS, оставляет уже-затронутых без начисления; сервер обязан быть устойчив к обоим идентификаторам. Серверный двухступенчатый резолв закрывает инцидент немедленно и корректен при любом поведении клиента (путь (a) поймает прямой userId).
- **Начислять `non_subscription_purchase` в этом же ADR.** Отвергнуто: отдельная ось (парсер + карта продуктов + анти-тампер), затрагивает монетизацию, требует решения владельца — вынесено в [Q-055-1](../99-open-questions.md).
</content>
</invoke>
