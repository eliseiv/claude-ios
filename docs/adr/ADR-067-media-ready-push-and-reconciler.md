# ADR-067 — Push при готовности media + фоновый reconciler

- **Статус:** Accepted
- **Дата:** 2026-08-11
- **Связано:** [ADR-060](ADR-060-media-generation-fal.md) (poll-контракт media), [ADR-032](ADR-032-notifications-enabled-default-false.md) (toggle), [TD-011](../100-known-tech-debt.md), [Q-060-2](../99-open-questions.md), [modules/notifications/](../modules/notifications/00-overview.md), [modules/media-generation/](../modules/media-generation/README.md)

## Контекст

iOS замораживает приложение ~через 30 с после сворачивания. Клиентский polling `GET /v1/media/jobs/{id}` останавливается, а видео (Kling/Veo) генерируется минутами — без серверного сигнала пользователь не узнает о готовности.

До этого статуса:

- статус media двигался **только** при poll клиента ([ADR-060](ADR-060-media-generation-fal.md));
- таблица `device_push_tokens` и APNs-отправка были только в docs ([TD-011](../100-known-tech-debt.md));
- media **не** привязана к chat/conversation ([ADR-063](ADR-063-media-feed-edit-chains-and-job-deletion.md)).

## Решение

### 1. Регистрация токена

- `POST /v1/notifications/device-token` / `DELETE /v1/notifications/device-token` (контракт уже был в notifications/02).
- Привязка к JWT `sub` (= `userId`) + `deviceId` (body → JWT claim → `X-Device-Id`). Apphud id в бэке нет.
- Таблица `device_push_tokens` (миграция `0022`).

### 2. APNs при `completed`

После `mark_completed` в `MediaGenerationService._advance` (общий путь poll и reconciler):

- payload: `aps.mutable-content=1`, `aps.alert`, плюс custom `jobId`, `kind` (`image`|`video`), `mediaUrl` (первый `assets[].url`);
- deep link — по `jobId` (не `conversationId`);
- уважать `user_preferences.notifications_enabled` (default `false`, ADR-032);
- без `APNS_*` credentials — send no-op (warning), CRUD токена работает;
- ошибки APNs не откатывают completed; `410` → удалить токен;
- идемпотентность: `media_jobs.push_sent_at` claim (`UPDATE … WHERE push_sent_at IS NULL`).

Триггер только **`completed`**, не `failed`.

### 3. Фоновый reconciler (закрывает Q-060-2)

Asyncio-loop в lifespan API (`MEDIA_RECONCILE_INTERVAL_SECONDS`, default 15; `<=0` = off):

- выбирает non-terminal `media_jobs` (oldest first, batch size);
- вызывает тот же `_advance` / fal status+result path;
- обеспечивает refund + push, когда клиент ушёл в фон.

Env: `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_AUTH_KEY`|`APNS_AUTH_KEY_PATH`, `APNS_TOPIC`, `APNS_ENVIRONMENT`, `MEDIA_RECONCILE_*`.

## Отклонённое

- Путь `/api/users/device-token` и привязка к apphud_id — чужой контракт; у нас `/v1` + JWT.
- Webhook fal — отвергнут в ADR-060 (публичная callback-поверхность).
- Push на `failed` — вне scope (iOS просил «готово»).
- Поле `conversationId` в media — чата нет; клиент открывает ленту/карточку по `jobId`.

## Последствия

- Закрывает практическую часть [Q-060-2](../99-open-questions.md) и media-триггер [TD-011](../100-known-tech-debt.md).
- Per-instance нужны APNs `.p8` + topic (= bundle id); без них токены копятся, пуши не уходят.
- Multi-worker: несколько API-реплик могут гонять reconciler параллельно — `push_sent_at` и wallet idempotency ключи делают это безопасным.
