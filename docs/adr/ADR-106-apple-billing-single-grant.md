# ADR-106 — Одна оплата Apple — одно начисление: общий ключ периода, резолв по `profile_id`, устаревшие транзакции, незаведённый продукт, пакеты токенов через вебхук Adapty

- Статус: Accepted
- Дата: 2026-09-15
- Тип: bugfix + feature ADR (биллинг Apple: StoreKit `sync`, вебхук Adapty, покупка пакетов токенов)
- Модули: [billing-adapty](../modules/billing-adapty/README.md), [subscription](../modules/subscription/README.md), [token-purchase](../modules/token-purchase/README.md), [wallet-ledger](../modules/wallet-ledger/README.md)
- **Пересматривает (тела не переписаны, в шапках — ссылка сюда):** [ADR-006 §2](ADR-006-credit-billing-and-subscription-grant.md) (ключ гранта периода становится общим для двух каналов), [ADR-029](ADR-029-adapty-subscription-webhook.md) §Контекст/§6 (граница «consumable не через Adapty», мера «клиент использует один путь»), [ADR-047 §C](ADR-047-adapty-real-payload-format-and-grant-idempotency.md) (ключ гранта Adapty при настоящем `transaction_id`), [ADR-055 §B](ADR-055-adapty-webhook-user-resolution-via-auth-devices.md) (резолв пользователя: второй идентификатор `profile_id`), [ADR-046](ADR-046-adapty-webhook-outcome-logging.md) (две новые причины `ignored`, новые поля лога), [ADR-015](ADR-015-consumable-token-iap.md) §Разграничение (второй канал начисления пакета, без требования подписки).
- **Не пересматривает:** [ADR-005](ADR-005-idempotency-ledger.md) (механизм `ux_ledger_idempotency` и правило «тот же ключ + другая сумма → конфликт» у `WalletService.grant`), [ADR-099 §6](ADR-099-crm-admin-economics-and-instance-settings.md) (порядок «оверлей → карта канала → фолбэк канала» и суммы), [Q-015-1](../99-open-questions.md) (требование подписки у `POST /v1/tokens/purchase`), [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) (RU-путь, ключ `cp-txn:`).
- **Закрывает:** [Q-055-1](../99-open-questions.md), [Q-029-1](../99-open-questions.md); [TD-020](../100-known-tech-debt.md), [TD-021](../100-known-tech-debt.md) (с выкатом кода). **Заводит:** [TD-054](../100-known-tech-debt.md) (возвраты Apple), [TD-055](../100-known-tech-debt.md) (отметка незаведённого продукта в CRM).
- Миграции: **нет**. Новых env: **нет**.
- **Реализация не выполнена** (на дату принятия — docs-only) — задача `backend` + `qa`.

## Контекст

Разбор жалоб по двум инстансам (2026-09-14) выявил пять дефектов начисления. Факты о коде ниже сверены с деревом на `d99d662`; числа по проду взяты из разбора и этим документом **не перемерялись**.

1. **Одна Apple-транзакция начисляется дважды.** `SubscriptionService.sync` пишет грант под ключом `sub-grant:{transactionId}` (`src/app/subscription/service.py:82`), вебхук Adapty — под `adapty-txn:{txn}` (`src/app/billing_adapty/service.py:383`). Уникальный индекс `ux_ledger_idempotency (user_id, idempotency_key)` (`src/app/models/tables.py:132`) разные ключи не связывает. Прежняя мера была контрактной — «клиент использует один путь подписок» ([ADR-029](ADR-029-adapty-subscription-webhook.md), [TD-021](../100-known-tech-debt.md)) — и не выполнялась: по разбору пар «оба ключа по одной транзакции у одного пользователя» 83 на одном инстансе, 2 и 1 на двух других.
2. **Вебхук Adapty теряет событие без `customer_user_id`.** `parse_customer_user_id` читает только `customer_user_id`/`profile.customer_user_id`/`event_properties.customer_user_id`/`user_id` (`src/app/billing_adapty/parser.py:112-133`); при их отсутствии `handle()` возвращает `ignored/missing_customer_user_id` **до** резолва (`src/app/billing_adapty/service.py:160-171`). Приложение без `Adapty.identify` присылает только Adapty `profile_id`.
3. **Устаревшая транзакция переписывает подписку.** `SubscriptionService.sync` безусловно пишет `status`/`plan`/`expires_at` верифицированной транзакции (`src/app/subscription/service.py:53-68`); `AdaptyWebhookService._upsert_subscription` — так же для GRANTING и EXPIRING (`src/app/billing_adapty/service.py:329-367`). Транзакция более раннего периода, пришедшая позже, откатывает план и срок назад.
4. **Незаведённый продукт подписки молча получает фолбэк канала.** `subscription_credits` при отсутствии оверлея и записи в карте канала возвращает фиксированный фолбэк (`src/app/instance_config/products.py:268-302`); ни лога, ни события аудита нет.
5. **Пакет токенов, оплаченный через Adapty, не начисляется** ([Q-055-1](../99-open-questions.md)). `non_subscription_purchase` не входит в `KNOWN_EVENTS` (`src/app/billing_adapty/parser.py:24-28`) → `ignored` с эхом типа. Пакет начисляется, только если приложение само вызывает `POST /v1/tokens/purchase`.

Отдельно установлено по коду, что меняет форму решения §A: при конфликте ключа `WalletService.grant` сравнивает сумму уже записанной строки и **поднимает `ConflictError`** («idempotency key reused with different payload», `src/app/wallet/service.py:223-229`), а `ConflictError` — это HTTP `409` (`src/app/errors.py:86-88`). Суммы каналов по одному продукту калибруются **раздельно** ([ADR-099 §6](ADR-099-crm-admin-economics-and-instance-settings.md): StoreKit — `SUBSCRIPTION_CREDITS_PER_PERIOD`, Adapty — `ADAPTY_PRODUCT_TOKENS` → `ADAPTY_SUBSCRIPTION_TOKENS_GRANT`). Значит, один общий ключ без дополнительного правила превращает второй канал в `409` у `sync` и в не-`2xx` у вебхука — а не-`2xx` Adapty ретраит бесконечно.

## Решения владельца (приняты, дословно)

1. На вопрос о уже случившихся двойных начислениях выбран вариант «**Оставить, только отчёт (Рекомендую)** — Лишние токены не списываем, чтобы пользователи не увидели внезапно уменьшившийся или отрицательный баланс. В плане будет точный отчёт: пользователи, транзакции, сумма лишнего».
2. В объём, помимо трёх дефектов кода (двойное начисление, поиск пользователя Adapty по `profile_id`, старые транзакции в `subscription/sync`), включены: «**Не молчать о незаведённом продукте** — Подписка с незаведённым ID сейчас молча получает значение по умолчанию (на zenquelo это было 1000). Добавить предупреждение в лог и отметку в CRM, начисление при этом не блокировать» и «**Пакеты токенов через вебхук Adapty** — Открытый вопрос Q-055-1: начислять non_subscription_purchase по вебхуку, с общей с приложением защитой от двойного начисления. Токены будут приходить, даже если приложение не вызывает /v1/tokens/purchase». Вариант «RU-checkout принимает UUID товара» **не** выбран — в объём не входит.

## Решение

Несущий инвариант волны: **одна оплата Apple → ровно одно начисление по любому каналу, в пределах одного `userId`; оплаченная покупка не теряется из-за идентификации пользователя; ошибка каталога видна.**

### A. Один период Apple — один ключ гранта

**A1. Общий ключ.** Грант периода подписки по Apple-транзакции `T` пишется под ключом **`sub-grant:{T}`** обоими каналами:

| Канал | Ключ гранта | Проверяемые до гранта ключи |
|---|---|---|
| `POST /v1/subscription/sync` | `sub-grant:{transactionId}` (как сейчас) | `sub-grant:{T}`, `adapty-txn:{T}` |
| Вебхук Adapty, GRANTING, `transaction_id` есть | **`sub-grant:{transaction_id}`** (было `adapty-txn:`) | `sub-grant:{T}`, `adapty-txn:{T}` |
| Вебхук Adapty, GRANTING, `transaction_id` нет (фолбэк `original_transaction_id` ‖ `event_id`, [ADR-047 §C](ADR-047-adapty-real-payload-format-and-grant-idempotency.md)) | `adapty-txn:{фолбэк}` (**без изменений**) | `adapty-txn:{фолбэк}` |

Канонический ключ — только при настоящем `transaction_id`: фолбэк на `original_transaction_id` постоянен на всю цепочку и с периодом StoreKit не совпадает, связывать его с `sub-grant:` нельзя.

**A2. Период уже начислен ⇒ гранта нет, и это не ошибка.** Перед грантом канал проверяет наличие **любого** ключа из правого столбца через `WalletService.has_idempotency_key` (`src/app/wallet/service.py:103-112`). Есть хоть один → гранта нет. Проверка исторического `adapty-txn:{T}` обязательна: без неё повтор старого события по периоду, начисленному ещё под прежним ключом, начислил бы третий раз.

**A3. Гонка двух каналов закрывается индексом, а расхождение сумм — правилом исхода.** Оба канала по одному `T` приходят почти одновременно (приложение зовёт `sync` сразу после покупки, вебхук идёт следом), поэтому проверка A2 у обоих может пройти до записи. Второй `INSERT` уходит в `ON CONFLICT DO NOTHING` (`src/app/wallet/service.py:208-222`). Если суммы каналов разные, `WalletService.grant` поднимет `ConflictError`. **Норма:** на ключах периода (`sub-grant:`, `adapty-txn:`) и покупки (`token-purchase:`, §E) существующая строка `credit` под ключом означает «уже начислено» **независимо от суммы** — вызывающий канал обязан завершиться штатно: `sync` отвечает `200` текущим состоянием, вебхук — `applied`, `POST /v1/tokens/purchase` — `creditsAdded=0`. Ни `409`, ни не-`2xx` из-за расхождения сумм между каналами быть не должно. Контракт `WalletService.grant` ([ADR-005](ADR-005-idempotency-ledger.md)) при этом **не меняется**: правило исполняется на стороне вызывающего.

**A4. Сумма определяется первым дошедшим каналом.** Число кредитов периода = сумма того канала, чья строка записана первой. До этой волны пользователь получал сумму **обоих** каналов, поэтому любой исход не больше прежнего. Детерминированность суммы между каналами этим решением не вводится: калибровка каналов остаётся раздельной ([ADR-099 §6](ADR-099-crm-admin-economics-and-instance-settings.md)).

**A5. Область инварианта — один `userId`.** Уникальность ключа — по паре `(user_id, idempotency_key)`. Если `sync` (пользователь из JWT) и вебхук (пользователь из резолва §B) дали **разных** пользователей, два гранта остаются возможными — это не закрывается ни этим решением, ни индексом. Измерение по флоту — `next_actions` отчёта волны.

**A6. Свип по форме «ключ гранта по оплате стора».** `grep -rn 'idempotency_key=f"' src/app` на `d99d662` — 11 мест. Грант по транзакции стора дают три: `subscription/service.py:82`, `billing_adapty/service.py:383`, `token_purchase/service.py:87`. Не подпадают: `billing_cloudpayments/service.py:442` (`cp-txn:{payment_id}` — отдельный платёж broadapps, с транзакциями Apple не пересекается, не меняется), `admin/crm_service.py:934,944,1032` и `admin/service.py:178` (действие оператора, не оплата), `media_generation/service.py:303,715,759` и `api_gateway/routers/chat_voice.py:1286` (списания и возвраты).

**Потребители префиксов ключа** (свип по имени величины `sub-grant`/`adapty-txn`/`token-purchase` по `src/`, `tests/`, `migrations/`, `infra/`): вкладка «Оплаты» CRM фильтрует `ledger_transactions` только по `crm-sub-grant:%`/`crm-tokens:%`/`admin-sub-grant:%` (`src/app/admin/crm_service.py:236-243`) — Apple-гранты туда не входили и не входят; события Adapty (включая `non_subscription_purchase`) она читает из `adapty_webhook_events`. Префикс `sub-grant:%` шаблоном `LIKE 'crm-sub-grant:%'` не матчится. Тесты, закрепляющие прежние нормы, — §«Ожидаемые падения тестов» ниже.

### B. Пользователь Adapty резолвится и по `profile_id`

**B1. Парсинг.** Новый идентификатор `profile_id` = первое непустое из `profile_id` → `profile.profile_id` → `event_properties.profile_id`, приведённое к UUID; не-UUID → «нет». Порядок источников — тот же, что у `customer_user_id` ([ADR-047 §A](ADR-047-adapty-real-payload-format-and-grant-idempotency.md)); реальный payload несёт поле в `event_properties` ([billing-adapty/02](../modules/billing-adapty/02-api-contracts.md)).

**B2. Порядок резолва (Stage 2–3 `handle()`), первый успешный выигрывает:**

1. `customer_user_id` есть → `resolve_user(customer_user_id)`; найден → `resolvedFrom = customer_user_id`.
2. Иначе (нет `customer_user_id` **или** резолв его не нашёл) и `profile_id` есть → `resolve_user(profile_id)`; найден → `resolvedFrom = profile_id`.
3. Нет **ни одного** идентификатора → `ignored/missing_customer_user_id` (имя причины сохраняется ради дашбордов; смысл — «адресата в теле нет»).
4. Хотя бы один идентификатор был, но ни один не резолвнут → `ignored/user_not_found`.

Второго резолвера не заводится: `profile_id` идёт через тот же `resolve_user` (`src/app/billing_common/resolve.py:24-82`, ветви `users` → `lower(auth_devices.device_id)` → `legacy_user_ids`). Совпадение `profile_id` с `auth_devices.device_id` — свойство приложения (оно регистрирует устройство под Adapty `profile_id`); кодом сервиса оно не проверяется и подтверждается прогоном после выката.

**B3. Наблюдаемость.** В `adapty_webhook_outcome` добавляются поля **`resolvedFrom`** (`customer_user_id` \| `profile_id`; там же, где `resolvedVia`) и **`profileId`** (UUID, когда резолв шёл по `profile_id`, включая `user_not_found` на этой ветке). `customerUserId` сохраняет прежний смысл — только `customer_user_id` из тела. Audit `adapty_subscription` получает `resolvedFrom`; `customerId` = идентификатор, по которому пользователь резолвнут. Уровни не меняются.

### C. Устаревшая транзакция не переписывает подписку

**C1. Предикат устаревания** (один для всех путей, вычисляется из хранимой строки и пришедшего срока):

`stale := row существует ∧ row.status = active ∧ row.expires_at ≠ NULL ∧ incoming.expires_at ≠ NULL ∧ incoming.expires_at < row.expires_at`

- (а) против недооценки: транзакция более раннего периода при активной подписке с более поздним сроком — `stale`, строка не трогается;
- (б) против переоценки: продление (`incoming.expires_at > row.expires_at`), повтор того же периода (`=`), первая покупка (`row` нет) и покупка после истечения (`row.status = expired`) — **не** `stale`, обрабатываются как сейчас.

**C2. Что делает `stale`.** Строка `subscriptions` (`status`/`plan`/`expires_at`/`will_renew`) **не меняется**; в аудит пути пишется `stale: true`. **Грант решается не устареванием, а ключом периода (§A2):** активная, не отозванная и не заменённая апгрейдом транзакция, чей период ещё не начислен, начисляется. Причина: устаревшая по сроку транзакция может быть оплаченным периодом (подписка из другой группы, период после ручной выдачи плана с более поздним сроком); отказ в гранте был бы потерей оплаты, а двойное начисление уже исключено ключом.

**C3. Точки применения (свип по форме «запись срока подписки событием стора»):**

| Путь | Применяется | Поведение при `stale` |
|---|---|---|
| `SubscriptionService.sync` | да | строка не меняется; грант по §A2 при активной транзакции; ответ `200` = **текущее состояние строки** (`isSubscribed` = активна и не истекла, `expiresAt`/`plan` строки) |
| Adapty GRANTING (`_upsert_subscription`) | да | строка не меняется (срок назад не сдвигается, план не меняется); грант по §A2 |
| Adapty EXPIRING (`_upsert_subscription`) | да | строка **не** переводится в `expired`: истечение прошлого периода не отзывает доступ текущего |
| Adapty NOOP (`_read_subscription`) | да | `will_renew` не пишется: отмена автопродления прошлого периода не относится к текущему |
| CloudPayments (`_upsert_subscription`) | нет | срок вычисляется от момента обработки (`parser._compute_expiry(_now(), …)`, `src/app/billing_cloudpayments/service.py:434`), срока стора во входе нет — предикат не вычислим |
| Ручная выдача плана (CRM / admin) | нет | действие оператора, не событие стора |

**C4. Транзакция, заменённая апгрейдом, неактивна.** `StoreKitVerifier._normalize_payload` (`src/app/subscription/storekit.py:217-246`) читает `isUpgraded`; `true` → транзакция неактивна наравне с `revocationDate` (Apple заменил её более высоким уровнем): статус `expired`, гранта нет, в аудит — `upgraded: true`. Предикат C1 применяется к ней как к любой другой.

**C5. Граница.** Порядок GRANTING и EXPIRING **одного** периода (равные сроки) предикатом не упорядочивается — поведение прежнее.

### D. Незаведённый продукт подписки — видно, начисление не блокируется

**D1. Источник суммы.** `subscription_credits` возвращает сумму **и источник**: `overlay` \| `channel_map` \| `channel_fallback`. Порядок и суммы [ADR-099 §6](ADR-099-crm-admin-economics-and-instance-settings.md) не меняются.

**D2. Предикат «продукт не заведён» — функция пары (канал, источник):**

| Канал | `unmapped`, когда | Почему |
|---|---|---|
| `adapty` | источник = `channel_fallback` | у канала есть карта `ADAPTY_PRODUCT_TOKENS`; фолбэк = продукта нет ни в оверлее, ни в карте |
| `cloudpayments` | источник = `channel_fallback` | то же для `CLOUDPAYMENTS_PRODUCT_TOKENS` |
| `storekit` | источник = `channel_fallback` **и** продукта нет в объединённом каталоге инстанса (`find_product` → `None`) | у канала нет карты по построению ([ADR-099 §6](ADR-099-crm-admin-economics-and-instance-settings.md)): фолбэк — его штатная сумма, и предупреждение на каждом `sync` было бы шумом; ошибка каталога — продукт, которого инстанс не знает вовсе |
| `manual` | никогда | `product_id` проверяется по каталогу до выдачи (`known_product_ids`), а фолбэк для продуктов вне карты CloudPayments — штатная пара канала |

- (а) против недооценки: начисленный из фолбэка период продукта, которого инстанс не знает, **всегда** даёт сигнал;
- (б) против переоценки: сигнал только при **фактически созданной** строке гранта; повтор, «период уже начислен» (§A2/A3) и сумма из оверлея/карты сигнала не дают.

**D3. Сигнал.** WARNING `subscription_product_unmapped` с полями `channel`, `productId`, `amount`, `transactionId` и событие аудита `subscription_product_unmapped` (тот же payload, `user_id` получателя) в транзакции гранта. Начисление **не блокируется**: оплата уже прошла.

**D4. Граница волны.** Отметка в CRM требует расширения контракта CRM во втором репозитории — **в эту волну не входит** ([TD-055](../100-known-tech-debt.md)); здесь только лог и аудит.

### E. Пакеты токенов через вебхук Adapty (закрывает [Q-055-1](../99-open-questions.md))

**E1. Событие.** `non_subscription_purchase` добавляется в распознаваемые события как **отдельная** ветка: `subscriptions` не читает и не пишет, подписочный грант не вызывает.

**E2. Порядок проверок** (после резолва §B, **до** дедуп-`INSERT` — отказ не пишет в БД, как все `ignored`):

1. `transaction_id` нет → `ignored/missing_transaction_id` (без ключа однократность не гарантировать; фолбэк на `original_transaction_id`/`event_id` для покупки **не** применяется).
2. Сумма = **только** `one_time_credits(vendor_product_id)` (`src/app/instance_config/products.py:206-232`; оверлей `one_time` → `TOKEN_PRODUCTS`). `vendor_product_id` нет или сумма `None` → `ignored/unknown_product`. Сумма и количество из payload **никогда** не читаются (анти-тампер [ADR-015](ADR-015-consumable-token-iap.md) BR-TP-1).
3. Дедуп события → грант под ключом **`token-purchase:{transaction_id}`** — тем же, что пишет `POST /v1/tokens/purchase` (`_IDEMPOTENCY_PREFIX`, `src/app/token_purchase/service.py:43`), с правилом §A3. Порядок каналов не важен: второй получает «уже начислено».
4. `applied`; audit `adapty_subscription` с `semantics: "one_time_purchase"`, `productId`, `transactionId`, `resolvedFrom`.

Грант: `reason="token_purchase"`, `meta={source:"token_purchase", productId, transactionId, eventType}` — строка в истории неотличима по классу от покупки через приложение (BR-TP-3).

**E3. Осознанное отличие канала: активная подписка НЕ требуется.** `POST /v1/tokens/purchase` по [Q-015-1](../99-open-questions.md) отказывает без активной подписки (`403 subscription_required`). Вебхук начисляет пакет **без этой проверки** — это прямое следствие решения владельца №2 («Токены будут приходить, даже если приложение не вызывает /v1/tokens/purchase»): вебхук приходит после того, как Apple уже списал деньги, и отказ означал бы потерю оплаченной покупки. [Q-015-1](../99-open-questions.md) не пересматривается: приложение по-прежнему показывает покупку токенов подписчикам, а `POST /v1/tokens/purchase` сохраняет свой `403`. Наблюдаемые следствия: пользователь без подписки, купивший пакет, получает кредиты вебхуком, а его `POST /v1/tokens/purchase` отвечает `403`; подписчик, чей пакет уже начислил вебхук, получает от `POST /v1/tokens/purchase` `creditsAdded=0`.

**E4. Уровни лога.** `missing_transaction_id` и `unknown_product` — **WARNING**: деньги пользователя взяты, начисления нет ([ADR-046](ADR-046-adapty-webhook-outcome-logging.md), класс «потенциально потерянное начисление»). В этих исходах и в `applied` ветки покупки лог несёт `productId` и `transactionId` — без них оператор не восстановит покупку вручную; оба не PII.

**E5. Возвраты не обрабатываются.** `non_subscription_purchase_refunded`, `subscription_refunded` и прочие возвраты остаются вне распознаваемых событий: `ignored` с эхом типа, WARNING ([TD-054](../100-known-tech-debt.md)).

### Сводка наблюдаемых величин волны

| Величина | Производитель | Потребитель |
|---|---|---|
| ключ `sub-grant:{T}` у вебхука Adapty | `AdaptyWebhookService` (грант GRANTING) | `ux_ledger_idempotency`, проверка §A2 в `SubscriptionService.sync` |
| проверка `adapty-txn:{T}` | оба канала до гранта | историческая строка ledger |
| `reason` `missing_transaction_id`, `unknown_product` | ветка покупки `handle()` | тело ответа Adapty, `adapty_webhook_outcome` (WARNING) |
| поля `resolvedFrom`, `profileId` | резолв §B | `adapty_webhook_outcome`, audit `adapty_subscription` |
| `stale`, `upgraded` в аудите | §C | `subscription_change`, `adapty_subscription` |
| WARNING и аудит `subscription_product_unmapped` | точки гранта `sync` / Adapty / CloudPayments по предикату D2 | журнал инстанса, `audit_logs`; CRM — [TD-055](../100-known-tech-debt.md) |

## Последствия

- (+) Двойное начисление подписки между `sync` и вебхуком Adapty исключено кодом, а не договорённостью с клиентом; мера «клиент использует один путь» больше не нужна.
- (+) Платёж пользователя без `Adapty.identify` доходит, если его устройство зарегистрировано.
- (+) Пакет токенов начисляется, даже если приложение не вызвало `POST /v1/tokens/purchase`.
- (+) Ошибка каталога подписок видна в журнале и аудите.
- (−) Сумма периода зависит от того, какой канал пришёл первым (§A4).
- (−) Разные `userId` у двух каналов по одной транзакции по-прежнему дают два гранта (§A5).
- (−) Покупка пакета без подписки через вебхук начисляется, хотя `POST /v1/tokens/purchase` для того же пользователя отвечает `403` (§E3).
- Исторические двойные начисления **не списываются** (решение владельца №1): отчёт по ним — разовая операция вне кода и вне `docs/`. Новые ключи действуют только для новых событий; ретроактивных начислений и миграций нет.

## Ожидаемые падения тестов

Падение опознаётся **признаком**, а не перечнем; перечень — снимок `grep` по `tests/` на `d99d662`, он может быть неполон, и тест, подпадающий под признак, но здесь не названный, — то же ожидаемое падение (приведение к новой норме — зона `qa`):

1. тест утверждает ключ `adapty-txn:{T}` у GRANTING-события **с** `transaction_id` — снимок: `tests/integration/test_billing_adapty_adr047.py:279,513`, `test_billing_adapty_device_resolution_adr055.py:242,286,312`, `test_billing_adapty_uppercase_device_adr055.py:192` (`test_billing_adapty_webhook.py:375` — ключ фолбэка `event_id`, признаку **не** отвечает);
2. тест присылает `profile_id` без `customer_user_id` и ждёт `missing_customer_user_id` — после §B это `user_not_found` (или `applied`, если `profile_id` резолвим); файлы-кандидаты по `grep -l missing_customer_user_id` ∩ `grep -l profile_id`: `tests/integration/test_billing_adapty_adr047.py`;
3. тест вызывает `subscription_credits` и ждёт одно число — после §D возвращается и источник; снимок: `tests/unit/test_instance_config_products_adr099.py`, `tests/integration/test_grant_channel_fallbacks_adr099.py`;
4. тест ждёт `ignored` с эхом типа на `non_subscription_purchase` либо перезапись строки `subscriptions` событием/транзакцией с более ранним сроком при активной подписке — снимок `grep` пуст.

Падение по сигнатуре (тест зовёт символ, который реализация переименовала или пересигнатурила, при истинном утверждении теста) — сопровождение стража, а не новая норма.

## Альтернативы (отклонены)

- **Новый канонический префикс (`apple-sub:{T}`) для обоих каналов.** Отклонено: ключ `sub-grant:{transactionId}` уже означает ровно эту сущность — грант периода подписки по Apple-транзакции ([ADR-006 §2](ADR-006-credit-billing-and-subscription-grant.md)); новый префикс добавил бы третий исторический ключ к проверке §A2 и совпал бы по звучанию с `apple_sub` — субъектом Sign in with Apple ([ADR-043](ADR-043-sign-in-with-apple.md)).
- **Только уникальный индекс, без проверки §A2 и правила §A3.** Отклонено: разные суммы каналов дают `ConflictError` → `409` у `sync` и бесконечные ретраи вебхука; историческое `adapty-txn:` не ловится вовсе.
- **Ретировать `sync`** ([Q-029-2](../99-open-questions.md)). Отклонено для этой волны: не все приложения отправляют события в Adapty; вопрос остаётся открытым, но риск двойного начисления, ради которого он заводился, закрыт.
- **Устаревшая транзакция не начисляет вовсе.** Отклонено: оплаченный период с более ранним сроком (другая группа подписок, ручная выдача плана с поздним сроком) остался бы без кредитов; повтор уже исключён ключом.
- **Требовать подписку и у вебхука покупки.** Отклонено решением владельца №2: деньги уже списаны Apple.
- **Списать исторические двойные начисления.** Отклонено решением владельца №1.
