# API Gateway — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| GW-1 | App factory, config (pydantic-settings), `/health`, `/ready`. | — |
| GW-2 | Correlation id middleware + структурированное логирование с redaction. | GW-1 |
| GW-3 | JWT auth dependency (JWKS, RS256), `get_current_user`, сверка `userId==sub`. | GW-1, Q-005-1 (дефолт) |
| GW-4 | Size-лимиты (ASGI body + поле-специфичные валидаторы). | GW-1 |
| GW-5 | Rate limit middleware (Redis). | GW-1 |
| GW-6 | Регистрация роутеров модулей, response/error mapping (ADR-004). | GW-2..GW-5 |
| GW-7 | `/metrics` + Observability middleware (метрики/трейсы). | GW-2 |
| GW-8 | `GET /healthz` — алиас `/health` (`200 {status:"ok"}`, публичный, без auth) для healthcheck Traefik/smoke ([ADR-017](../../adr/ADR-017-shared-server-traefik-deploy.md)). Минимальная правка health-router + регистрация в `main.py`. | GW-1 |
| GW-9 | **Streaming body-guard + `413` без обрыва** ([ADR-089 §2](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md), закрывает [TD-017](../../100-known-tech-debt.md)): перевод `SizeLimitMiddleware` с `BaseHTTPMiddleware` на чистый ASGI; счётчик фактически прочитанных байт (работает без `Content-Length`); ограниченный drain остатка тела (`SIZE_LIMIT_DRAIN_BYTES`, дефолт 1 MiB) + `Connection: close` перед закрытием; `message` ответа содержит действующий лимит роута из `Settings`. Тело ответа и `code` не меняются. | GW-4 |
| GW-10 | **Карта повышенных лимитов по инварианту** ([ADR-089 §1](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md)): в множество путей с `ATTACHMENT_REQUEST_BODY_LIMIT` добавить `/v1/chat/v2/run/stream`; закрепить тестом-детектором «каждый роут с телом-подклассом `ChatRunRequest` получает повышенный лимит». Прочие ветки `_limit_for` (media uploads, admin templates, workspace files) не трогаются. | GW-9 |

Порядок реализации backend: GW-1..GW-3 — до любых бизнес-модулей (нужны auth и роутинг).
