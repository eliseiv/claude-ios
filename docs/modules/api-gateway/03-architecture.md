# API Gateway — Architecture

## Middleware-цепочка (порядок)
1. **Size limit** — отсекает тело > лимита до парсинга (`413`).
2. **Correlation id** — `requestId` из `X-Request-Id` или генерация; кладётся в context-var, попадает во все логи/трейсы. Это исключительно correlation id одного HTTP-запроса; биллинг-идемпотентность к нему не привязана (она использует `messageStepId`, [ADR-005](../../adr/ADR-005-idempotency-ledger.md)).
3. **Auth (JWT) + lazy provisioning** — проверка подписи (RS256, по `JWT_PUBLIC_KEY`/`JWT_JWKS_URL`), `exp/iss/aud`; извлечение `sub`, `device_id` (`401`). Затем, **в `get_current_user` после успешной верификации и до downstream**, идемпотентный upsert строки `users` для `sub`: `INSERT INTO users (id) VALUES (:sub) ON CONFLICT (id) DO NOTHING` — гарантирует существование родителя для всех FK-зависимых вставок (race-free). Источник истины идентичности — **встроенный issuer** ([ADR-018](../../adr/ADR-018-embedded-auth-issuer.md), закрывает Q-005-1; verify-only внешний issuer сохраняется как опция), `users.id ≡ sub`. См. [ADR-007](../../adr/ADR-007-lazy-user-provisioning.md), [05-security.md](../../05-security.md#модель-идентичности-и-провижининг-пользователей).
   - **Исключение `/v1/auth/*`** ([ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)): эти маршруты — точка выпуска токена, проходят **без** JWT-шага; их защищает per-IP rate-limit (шаг 4). `register` провижинит `users` **явно** (eager) тем же idempotent upsert; lazy-provisioning остаётся fallback для прочих путей. Двойной provisioning безопасен (`ON CONFLICT DO NOTHING`).
4. **Rate limit** — Redis sliding window per user/device/IP (`429`).
5. **Routing → handler** — Pydantic-валидация тела (`extra=forbid`, `422`); сверка `userId==sub` (`403`).
6. **Response mapping** — бизнес-200 vs тех. ошибки; redaction секретов в логах.
7. **Metrics/trace** — фиксация латентности, span.

```mermaid
flowchart LR
    REQ[Request] --> SZ[Size limit] --> CID[Correlation id] --> AUTH[JWT auth] --> RL[Rate limit] --> H[Handler/Router] --> RESP[Response mapping]
```

## Rate limiting
- Алгоритм: sliding window log / token bucket в Redis (ключи `rl:user:<id>`, `rl:dev:<id>`, `rl:ip:<addr>`).
- Лимиты из config/env (дефолты — [05-security.md](../../05-security.md), значения — [Q-003-1](../../99-open-questions.md)).

## Size-лимиты
- Глобальный body-лимит на ASGI-уровне (`SIZE_LIMIT_BODY`, дефолт 512 KB).
- Поле-специфичные лимиты (`message`, `context`, `result`) проверяются в Pydantic-валидаторах соответствующих схем.

### Per-path transport body-limit (`SizeLimitMiddleware._limit_for`)
`SizeLimitMiddleware` применяет общий `SIZE_LIMIT_BODY` ко всем путям, **кроме** роутов с inline base64, которым нужен повышенный лимит (крупный файл в base64 превышает 512 KB). Метод `_limit_for(path)` выбирает лимит по пути:

| Правило сопоставления пути | Лимит (конфиг) | Дефолт | Роут | ADR |
|---|---|---|---|---|
| `path in {"/v1/chat/run", "/v1/chat/v2/run", "/v1/chat/v2/run/stream"}` (точное) | `attachment_request_body_limit` (`ATTACHMENT_REQUEST_BODY_LIMIT`) | 80 MiB | все роуты генерации, принимающие `attachments[]` | [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), [ADR-089 §1](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md) |
| `path == "/v1/media/uploads"` (точное) | `media_upload_request_body_limit` (`MEDIA_UPLOAD_REQUEST_BODY_LIMIT`) | 16 MB | `POST /v1/media/uploads` | [ADR-062](../../adr/ADR-062-media-upload-via-fal-storage.md) |
| `path == "/v1/admin/media/templates"` (точное) | `media_template_cover_request_body_limit` (`MEDIA_TEMPLATE_COVER_REQUEST_BODY_LIMIT`) | 4 MB | `POST /v1/admin/media/templates` | [ADR-066](../../adr/ADR-066-media-templates-catalog.md) |
| `path.startswith("/v1/workspaces/") and path.endswith("/files")` | `workspace_request_body_limit` (`WORKSPACE_REQUEST_BODY_LIMIT`) | 12 MB | `POST /v1/workspaces/{id}/files` | [ADR-045](../../adr/ADR-045-per-path-body-limit-workspace-files.md) |
| иначе | `size_limit_body` (`SIZE_LIMIT_BODY`) | 512 KB | все прочие | — |

> **Карта — производная от инварианта, а не сам инвариант ([ADR-089 §1](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md)).** Повышенный `ATTACHMENT_REQUEST_BODY_LIMIT` действует на **каждом** роуте, тело которого может содержать `attachments[]`. Список путей — следствие; ведение списка вручную уже дало дефект: `/v1/chat/v2/run/stream` принимает тот же `ChatV2RunRequest`, но в множество не входил и тихо резался общим 512 KB. Инвариант закреплён **тестом-детектором**: для каждого роута, чьё тело — `ChatRunRequest` или подкласс, `_limit_for(path)` обязан вернуть `attachment_request_body_limit`.

**Точность правила workspace-files (важно):** префикс+суффикс матчит **именно** upload (`POST …/files`). НЕ задеты:
- CRUD `/v1/workspaces`, `/v1/workspaces/{id}` — нет суффикса `/files` → 512 KB (корректно, тела мелкие);
- `DELETE /v1/workspaces/{id}/files/{file_id}` — оканчивается на `/{file_id}`, не на `/files` → 512 KB;
- `GET /v1/workspaces/{id}/files` (список) — оканчивается на `/files`, попадает под повышенный лимит, но безвредно (GET-тело пустое). Матч метод-агностичен (как и `/v1/chat/run`); проверка метода не вводится — единственный путь с непустым телом под суффиксом `/files` — POST upload.

**Инвариант источника истины** ([ADR-045 §1](../../adr/ADR-045-per-path-body-limit-workspace-files.md)): `WORKSPACE_REQUEST_BODY_LIMIT ≥ WORKSPACE_FILE_MAX_BYTES*4/3 + JSON-запас(≥256 KB)`. Per-file потолок — единственный источник истины (`WORKSPACE_FILE_MAX_BYTES`=8 MB, [ADR-036 §4](../../adr/ADR-036-workspaces-implementation.md)); транспортный лимит производен от него (симметрично `ATTACHMENT_MAX_BYTES_DOCUMENT` ↔ `ATTACHMENT_REQUEST_BODY_LIMIT` и `MEDIA_UPLOAD_MAX_BYTES` ↔ `MEDIA_UPLOAD_REQUEST_BODY_LIMIT`). Memory-DoS guard сохранён: повышение точечно, прикладной size-cap (`validate_and_extract`) режет до 8 MB **до** base64-decode. Реализация — `src/app/api_gateway/middleware.py::SizeLimitMiddleware._limit_for`.

### Отдача `413` без обрыва соединения ([ADR-089 §2](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md), закрывает [TD-017](../../100-known-tech-debt.md))

Прежняя реализация (`BaseHTTPMiddleware`) отвечала `413`, **не прочитав тело**: клиент в этот момент ещё передавал байты, сервер закрывал соединение, и приложение показывало пользователю «нет связи» вместо «файл слишком большой». Плюс проверка опиралась на `Content-Length` и полностью пропускалась при его отсутствии.

Нормативное поведение:

1. **Middleware — чистый ASGI** (`__call__(scope, receive, send)`), не `BaseHTTPMiddleware`.
2. **Ветка A (ранняя):** `Content-Length` присутствует и больше `_limit_for(path)` → `413` немедленно.
3. **Ветка B (потоковая):** `receive` оборачивается счётчиком фактически прочитанных байт; превышение лимита роута → приложение к телу больше не допускается, отдаётся `413`. Работает и для `Transfer-Encoding: chunked` без `Content-Length` — транспортный guard перестаёт зависеть от заголовка, который контролирует клиент.
4. **В обеих ветках перед закрытием:** ответ отдаётся с `Connection: close`; остаток тела **вычитывается и отбрасывается**, но не более `SIZE_LIMIT_DRAIN_BYTES` (дефолт 1 MiB) сверх прочитанного; исчерпан бюджет — соединение закрывается. Drain нужен ровно затем, чтобы клиент дописал запрос и **прочитал ответ**; безлимитный drain отменял бы сам смысл лимита.
5. **Тело ответа не меняется** (совместимость): `{"error":{"code":"payload_too_large","message":…,"requestId":…}}`; `message` детерминированно содержит действующий лимит роута в байтах — то же значение `_limit_for(path)`, что применено при проверке, а не второй литерал. Машиночитаемое поле лимита — [Q-089-1](../../99-open-questions.md).

## Зависимости реализации
- FastAPI dependencies: `get_current_user`, `get_db`, `get_redis`, `require_owner`.
- Без бизнес-логики: handler делегирует в use-case модуля.
