# Admin — RBAC

## Принципал
- `admin` — обезличенный оператор, авторизуется изолированным `X-Admin-Token` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)).
  Не имеет `userId`/`sub`, не является пользователем системы.

## Правила
- Доступ к `/v1/admin/*` — **только** при валидном admin-секрете в `X-Admin-Key` (CRM) либо в легаси
  `X-Admin-Token` (зависимость `require_admin`, `src/app/api_gateway/auth.py:126-141`). Ни одного из
  заголовков не передано → **`403`**; заголовок есть, значение не совпало → **`401`**; секрет на
  инстансе не сконфигурирован → **`401`** (fail-closed).
- Пользовательский JWT (`Authorization: Bearer`) **не** даёт доступа к admin-роутам и не является фактором авторизации на них.
- Admin-токен **не** даёт доступа к пользовательским ресурсам через пользовательские эндпоинты (`/v1/chat/*`, `/v1/wallet`, …) —
  там по-прежнему требуется JWT и сверка `sub`.
- Эскалация невозможна: разные секреты, заголовки, зависимости (ADR-009 §4).
- Admin действует **над** `userId` из тела (`grant`)/пути (`get-wallet`) — это легитимно **только** на admin-роутах;
  на пользовательских роутах действие за другого `userId` запрещено (`403`, [05-security.md](../../05-security.md)).

## Изоляция инвариантов
- `require_admin` не запускает provisioning ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) и не трогает `trial`.
- Мутирующие admin-операции: начисление кредитов (`wallet/grant`), активация/продление подписки
  (`subscription/grant`, [ADR-048](../../adr/ADR-048-admin-subscription-grant.md)) и мутирующие
  CRM-ручки ([ADR-077](../../adr/ADR-077-crm-request-logs.md) и далее). Admin-списаний, правок
  BYOK/trial и удаления пользователей нет ([00-overview.md](00-overview.md)).
- **`GET /v1/admin/costs/daily` ([ADR-092](../../adr/ADR-092-crm-daily-costs-endpoint.md)) — строго
  read-only:** не мутирует состояние, не пишет audit, не действует над конкретным `userId` вовсе
  (агрегат по всему инстансу за период). Персональных данных в ответе нет — только дата, сырой ключ
  провайдера и три числа.

## Аудит
- Каждый `grant` → audit-событие `admin_grant` (actor=admin, `userId`, `amount`, `reason`, `idempotencyKey`, `ledgerTxId`).
  Секрет `X-Admin-Token` в audit/логи не попадает.
