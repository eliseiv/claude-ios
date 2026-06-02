# ADR-004 — HTTP 200 для бизнес-blocked + стандартизированный blockReason enum

- Статус: Accepted
- Дата: 2026-05-21

## Context
ТЗ §9 требует: HTTP 200 для бизнес-ответов со `status=blocked`; 4xx/5xx — только для технических ошибок API. UI должен машиночитаемо понимать причину блокировки.

## Decision
- Бизнес-блокировка генерации — это **успешный ответ** оркестрации: `200 OK` с телом `{status: "blocked", blockReason, sessionId?}`.
- 4xx/5xx — только технические ошибки: `401` (auth), `403` (чужой userId), `404` (нет ресурса), `409` (конфликт идемпотентности с другим payload), `413` (size), `422` (валидация), `429` (rate limit, техническое превышение), `5xx` (внутренние/внешние сбои).

### blockReason enum (зафиксирован)
```
trial_used
subscription_required
subscription_expired
credits_empty
byok_disabled
byok_invalid
rate_limited
policy_denied
```

- `rate_limited`: **gateway-concern**. При превышении rate limit API Gateway отдаёт HTTP `429` (стандартный error-формат с `code=rate_limited`). `rate_limited` — значение blockReason enum для HTTP-слоя/`/chat/run`, но **НЕ входит** в `/policy/effective.reasons[]` (BLK-7b): Policy Engine не знает rate-limit состояния, оно не часть `PolicyState` (ADR-002). «Мягкого» варианта на стороне оркестрации/policy нет — rate_limited всегда транспортный `429`, а не policy-reason.
- `policy_denied` — общий fallback для непредвиденных состояний Policy Engine.

### /policy/effective.reasons[]
Содержит подмножество enum, **вычислимое `evaluate` (ADR-002)**: `trial_used | subscription_required | subscription_expired | credits_empty | byok_disabled | byok_invalid | policy_denied` — причины, по которым соответствующий `canGenerate*` = false, чтобы UI и `/chat/run` были консистентны (AC-6). `rate_limited` сюда **не входит** (gateway-concern, BLK-7b).

## Consequences
- (+) Клиент различает «нельзя по бизнесу» (200) и «ошибка запроса» (4xx/5xx) однозначно.
- (+) Единый enum переиспользуется в `/chat/run`, `/chat/tool-result`, `/policy/effective`.
- (−) `200` с `blocked` нестандартно для REST-пуристов; задокументировано как осознанное правило домена.

## Alternatives
- `403`/`402 Payment Required` для бизнес-блокировок — отвергнуто: противоречит ТЗ §9 и смешивает бизнес-состояние с тех. ошибкой.
