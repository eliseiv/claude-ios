# Admin — Overview

## Назначение
Операторская/саппорт-функция: начисление кредитов пользователю вне обычного биллинг-потока подписки
(компенсации, ручные гранты, поддержка) и read-only просмотр кошелька для разбора обращений.

## Scope (этот проход)
- `POST /v1/admin/wallet/grant` — начислить `amount` кредитов пользователю `userId`, идемпотентно по `idempotencyKey`,
  с обязательным `reason`. Переиспользует существующий `WalletService.grant()` (`src/app/wallet/service.py:174`).
- `GET /v1/admin/wallet/{userId}` — баланс + последние ledger-транзакции (read-only, для саппорта).
- Изолированная admin-авторизация: `X-Admin-Key` (CRM) / легаси `X-Admin-Token` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)), зависимость `require_admin`.
- Аудит `admin_grant`, отдельный rate limit, strict validation, size-лимиты.

## Scope (2026-08-26, [ADR-092](../../adr/ADR-092-crm-daily-costs-endpoint.md))
- `GET /v1/admin/costs/daily` — **read-only** периодная разбивка расходов на AI-провайдеров:
  клетка `(день, провайдер)` с `spend_usd` / `requests` / `tokens`. Реализация замороженного
  контракта CRM v1.3; имена пути, параметров и полей из этого репозитория не меняются.
  Переиспользует закупочный прайс [ADR-079](../../adr/ADR-079-crm-provider-cost-duration-payments.md)
  (`app.pricing.provider_prices`), своей копии цен не заводит. Мутаций не выполняет, audit не пишет.

## Out of scope
> **Список ниже относится к первому проходу модуля (2026-06-01) и с тех пор частично снят
> решениями:** `POST /v1/admin/subscription/grant` ([ADR-048](../../adr/ADR-048-admin-subscription-grant.md))
> и мутирующие CRM-ручки ([ADR-077](../../adr/ADR-077-crm-request-logs.md) / [ADR-078](../../adr/ADR-078-crm-request-history-derived-from-domain.md) / [ADR-079](../../adr/ADR-079-crm-provider-cost-duration-payments.md))
> добавили admin-операции сверх начисления кредитов. Первоисточник действующего перечня
> `/v1/admin/*` — [02-api-contracts.md](02-api-contracts.md) и названные ADR, а не этот список.

- Admin-UI (только HTTP API).
- Персональная идентичность/атрибуция конкретного оператора (actor — обезличенный `admin`, [Q-009-1](../../99-open-questions.md)).
- Scope/least-privilege на уровне токена (один секрет на все admin-операции, [ADR-009 §Consequences](../../adr/ADR-009-admin-token-auth.md)).

## Бизнес-правила
- BR-ADM-1: admin **не пользователь системы** — `require_admin` не создаёт строку `users` для actor'а, не запускает
  lazy-provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)), не читает/не трогает `users.trial_used`.
- BR-ADM-2: начисление идемпотентно по `idempotencyKey` (через `WalletService.grant`, unique index `ux_ledger_idempotency`);
  повторный вызов с тем же ключом и payload → тот же `ledgerTxId`, `idempotentReplay=true`, без повторного начисления.
- BR-ADM-3: `reason` обязателен и пишется в audit `admin_grant` (и в `ledger_transactions.meta`, без секретов).
- BR-ADM-4: целевой `userId` **должен существовать** — admin-grant не создаёт пользователей (см. 03-architecture §Несуществующий userId).
