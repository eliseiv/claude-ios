# ADR-111 — RU-отмена подписки пишет `will_renew=false` только когда поставщик нашёл активную RU-подписку

- **Статус:** Accepted. **Состояние реализации на 2026-09-24T18:05Z, поэлементно:** (1) **код написан** — `git diff --stat -- src`: `src/app/api_gateway/routers/billing_cloudpayments.py` (`cloudpayments_cancel`: `if sub is not None and result.found:` и `willRenew=will_renew`) (рабочее дерево); (2) **в `main` не слито** — `git status --porcelain -- src` даёт ` M` по этим файлам, `HEAD` = `67bfd66`, изменения не закоммичены; (3) **не выкачено** — следует из (2); (4) **автотесты** — `grep -rln 'ADR-111\|adr111' tests` пуст на момент записи, тесты пишутся (`qa`), покрытие не измерено; (5) **ревью кода** — не измеряется.
- **Переснято 2026-09-24T18:16Z, поэлементно (строка выше — измерение на 18:05Z):** (1) код — в `main`: коммит `a985f30` (`git branch --contains a985f30` → `main`, `origin/main`); (2) выкачено — **нет на момент измерения**: прогон CI `36039561564` на `a985f30` — `in_progress` (`gh run list --commit`); (3) автотесты — `tests/integration/test_billing_cancel_adr111.py`, 4 функций `test_` (`grep -c 'def test_'`), покрытие не измерено; (4) ревью кода — не измеряется.
- **Дата:** 2026-09-24
- **Тип:** fix-ADR (модуль [billing-cloudpayments](../modules/billing-cloudpayments/README.md)); закрывает [TD-064](../100-known-tech-debt.md).
- **Пересматривает:** поведение «Эффект у нас» ручки `POST /v1/billing/cloudpayments/cancel`, внесённое в `docs/` по коду [ADR-110](ADR-110-ru-payment-neutral-path-aliases.md) (уточнение факта, нормой оно там не объявлялось, а было помечено ⚠️). Решения [ADR-110](ADR-110-ru-payment-neutral-path-aliases.md) (пары путей, паритет, одна функция-обработчик) не меняются.

## Контекст

- Строка `subscriptions` одна на пользователя (`user_id` — PK), колонки источника подписки нет (класс `Subscription`, `src/app/models/tables.py`). Её пишут и Apple/Adapty-пути, и RU-путь.
- `CloudPaymentsCheckoutClient.cancel_subscription` (`src/app/billing_cloudpayments/checkout.py`) возвращает `CancelResult(found=False)`, когда у поставщика нет записи `status=="active"` с непустым `subscription_id` — отмена у поставщика не вызывается; `found=True` — только после успешного (2xx) `POST …/subscriptions/{id}/cancel`. Собственный docstring метода гласит «`found=False` (no upstream cancel, caller no-ops)», а ручка тем не менее пишет флаг — код расходится с собственным контрактом клиента.
- Итог: пользователь с подпиской Apple/Adapty, нажавший RU-отмену, получает `will_renew=false` на ЧУЖОЙ подписке, и `/policy/effective` показывает «не продлится».

## Решение

1. **Предикат записи.** `subscriptions.will_renew=false` пишется ТОГДА И ТОЛЬКО ТОГДА, когда `cancel_subscription` вернул `found=True` (поставщик подтвердил, что у этого `user_id` была активная RU-подписка, и отменил её) И локальная строка `subscriptions` существует. `status`/`expires_at` не меняются (как и сегодня).
2. **`found=False` → локальное состояние не трогается** ни в одной колонке.
3. **Поле ответа `willRenew`** перестаёт быть константой и равно значению флага ПОСЛЕ операции: `found=True` → `false`; `found=False` → текущее `subscriptions.will_renew` строки; строки нет → `false`. Остальные поля ответа (`canceled`, `status`, `canceledAt`, `alreadyCanceled`) не меняются. Это изменение наблюдаемого ответа ровно на ветви `found=False` при существующей строке с `will_renew=true`; на прочих ветвях тело бит-в-бит прежнее. **Уточнение факта 2026-09-24, решение не меняется:** колонка `subscriptions.will_renew` допускает `NULL` (класс `Subscription`, `src/app/models/tables.py`: `Mapped[bool | None]`, `nullable=True`), а поле ответа `willRenew` — не nullable `bool` (`CloudPaymentsCancelResponse`, `src/app/schemas/billing_cloudpayments.py`). При `found=False` и `will_renew IS NULL` строка не трогается, а в ответ идёт `false` (`bool(None)`) — это норма: «продление не подтверждено» отдаётся как `false`, флаг в БД остаётся `NULL`. Контраст: `/policy/effective` отдаёт то же значение как есть, `null`, потому что его поле nullable (`src/app/policy/loader.py`: `subscription_will_renew: bool | None`).
4. **Отказ поставщика** (`502 upstream_error`) — локальное состояние не трогается (как и сегодня: исключение поднимается до записи).
5. **Оригинал и дубликат — одно поведение:** `/v1/web/cancel` регистрирует ту же функцию ([ADR-110 §1](ADR-110-ru-payment-neutral-path-aliases.md)), поэтому норма действует на оба пути без второй реализации.

**Предикат, обе стороны (по фактам пути):**
- (а) против недооценки: поставщик подтвердил отмену (`found=True`) ⇒ флаг обязан стать `false` — иначе `/policy/effective` обещает продление RU-подписки, которой больше нет;
- (б) против переоценки: поставщик не нашёл активной RU-подписки (`found=False`) ⇒ отменено ничего не было ⇒ флаг не меняется.

**Остаточный риск, не закрываемый этим ADR:** у пользователя одновременно активны RU-подписка и Apple/Adapty-подписка, а строка `subscriptions` отражает Apple → `found=True` всё равно сбросит флаг Apple-подписки. Строку пишут два RU-пути: эта ручка и webhook-upsert оплаты (`src/app/billing_cloudpayments/service.py`, `ON CONFLICT … DO UPDATE SET … expires_at = EXCLUDED.expires_at, will_renew = true`), а также пути Apple/Adapty (`src/app/billing_adapty/service.py`). Отличить источник без колонки источника нельзя; колонка — миграция схемы, решение владельца — [Q-111-1](../99-open-questions.md). До ответа действует п.1 без проверки источника.

## Альтернативы

- **Оставить безусловную запись** — отклонено: ломает `/policy/effective` для не-RU подписчиков (TD-064).
- **Проверять источник по косвенным признакам** (формат `product_id`, наличие Apple-транзакций) — отклонено: это суждение, а не предикат по наблюдаемому факту; признаки не нормированы.
- **Колонка источника в `subscriptions`** — не отклонена, вынесена владельцу ([Q-111-1](../99-open-questions.md)): миграция + правка всех писателей строки.

## Последствия / тесты

- Кейс `found=False` при СУЩЕСТВУЮЩЕЙ строке `subscriptions` с `will_renew=true` теперь ЗАКРЕПЛЯЕТ: `will_renew` остаётся `true`, ответ `canceled=false`, `willRenew=true`; на обоих путях пары.
- Кейс `found=True` при существующей строке: `will_renew=false`, `status`/`expires_at` без изменений, `willRenew=false`.
- Кейс `found=False` без строки: `willRenew=false`, строка не создаётся.
- Diff-стойкость: возврат безусловной записи роняет первый кейс.
- Зона: код — `backend`, тесты — `qa`; перечень мест — в [TD-064](../100-known-tech-debt.md) (закрыт этим решением нормативно).
