# billing-cloudpayments / 06 — RBAC / Authorization

## Checkout — пользовательский JWT ([ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md))
Эндпоинт `POST /v1/billing/cloudpayments/checkout` — **обычный пользовательский `/v1/*` контур** (`bearerAuth`, `CurrentUser`), НЕ machine-to-machine.

| Аспект | Значение |
|---|---|
| Механизм | Пользовательский JWT (RS256), `Authorization: Bearer <JWT>`; нет/невалидный → `401` |
| Идентичность | **`userId` = JWT `sub`** (`current.user_id`), **НЕ из тела** — ключевая мера (устраняет клиент-контролируемый `user_id`). Тело не содержит `userId`/`appId` |
| Провижининг | `get_current_user` лениво provision `users[sub]` ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)) → до оплаты гарантирует, что колбэк найдёт пользователя |
| Rate-limit | `enforce_other_limits(user_id=sub)` → `429` |
| Исходящая авторизация | к broadapps — серверный `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>` (**отдельный** от входящего `CLOUDPAYMENTS_WEBHOOK_TOKEN`; разные роли: мы→broadapps vs broadapps→мы) |
| Секреты | `CLOUDPAYMENTS_API_TOKEN` (секрет) и `CLOUDPAYMENTS_APP_ID` — серверные, не в клиенте, не в логах/ответе. `customer_email` — PII, не логируется |
| Не сконфигурировано | `CLOUDPAYMENTS_APP_ID`/`CLOUDPAYMENTS_API_TOKEN` пусты → `503` ⇒ активен только на avelyra |
| SSRF | исходящий вызов только к фиксированному `CLOUDPAYMENTS_API_BASE` (config), не из тела клиента |

## Webhook — machine-to-machine bearer
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
