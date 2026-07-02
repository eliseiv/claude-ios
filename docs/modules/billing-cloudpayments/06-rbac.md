# billing-cloudpayments / 06 — RBAC / Authorization

## Контур авторизации
Эндпоинт `POST /v1/billing/cloudpayments/webhook` — **пятый** machine-to-machine контур (наряду с пользовательским JWT, admin-токеном, preview signed URL, Adapty-webhook). Вызывается только агрегатором broadapps.

| Аспект | Значение |
|---|---|
| Механизм | статический bearer `Authorization: Bearer <CLOUDPAYMENTS_WEBHOOK_TOKEN>` |
| Сравнение | constant-time `hmac.compare_digest` (образец `require_adapty_webhook`) |
| Нет токена / mismatch | `401` (причина не раскрывается) |
| Секрет не сконфигурирован | `500` (мис-конфигурация; ⇒ эндпоинт активен только там, где секрет задан) |
| Изоляция | секрет отдельный от JWT, `ADMIN_API_SECRET`, KMS, `PREVIEW_URL_SECRET`, `ADAPTY_WEBHOOK_SECRET` |
| Идентичность пользователя | НЕ из токена; из тела (`AccountId`/`Data.user_id` = UUID, нормализованный к lower) |
| Per-instance | у каждого инстанса свой `CLOUDPAYMENTS_WEBHOOK_TOKEN`; задан только на avelyra ([ADR-017](../../adr/ADR-017-shared-server-traefik-deploy.md)) |

## Что эндпоинт НЕ делает
- НЕ принимает пользовательский JWT, НЕ создаёт/провижинит пользователя из тела.
- НЕ даёт admin-привилегий; секрет не пересекается с admin/Adapty-контурами (нет эскалации).
- НЕ доверяет `AccountId` как авторизации действий — это лишь адресат гранта; несуществующий → `200 {"code":0}` (`ignored/user_not_found`), без создания пользователя.
- НЕ обрабатывает рефанды (агрегатор их не шлёт).

## Аутентичность payload
- broadapps/CloudPayments **не подписывает** тело (нет HMAC подписи payload). Аутентичность = знание bearer-секрета (shared secret из панели broadapps). Митигация: высокоэнтропийный секрет, TLS на edge, per-instance, дедуп по `TransactionId`. То же ограничение, что у Adapty-контура ([05-security.md](../../05-security.md)).

## Реализация
Per-route dependency (Depends), не глобальный middleware. OpenAPI security-схема — http bearer с `auto_error=False` (образец `adapty_webhook_scheme`).
