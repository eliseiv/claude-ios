# Notifications — Architecture

## Размещение
Пакет `src/app/notifications/`: repository (`device_push_tokens`) + token CRUD + `ApnsClient` + `MediaPushService`. Роутер `/v1/notifications/*`. Toggle — preferences.

## Регистрация токена
- `deviceId` резолвится: тело → JWT-claim → `X-Device-Id`; отсутствие → `422`.
- Upsert: `INSERT ... ON CONFLICT (user_id, device_id) DO UPDATE SET push_token, updated_at`.

## Отправка (ADR-067)
- APNs token-based JWT (`APNS_KEY_ID` / `APNS_TEAM_ID` / `APNS_AUTH_KEY`|`_PATH` / `APNS_TOPIC` / `APNS_ENVIRONMENT`).
- Перед отправкой — `notifications_enabled`; выключено → skip.
- Триггер: `MediaGenerationService._advance` после `mark_completed` (poll **и** reconciler).
- Идемпотентность: `UPDATE media_jobs SET push_sent_at WHERE push_sent_at IS NULL`.
- `410 Unregistered` → delete token row(s) with that `push_token`.
- Ошибка APNs не откатывает completed.

## Media reconciler
- `src/app/media_generation/reconciler.py`, старт из lifespan при `MEDIA_RECONCILE_INTERVAL_SECONDS > 0`.
- Выборка non-terminal jobs → тот же `_advance`.
- **Сборка сервиса и дедлайн — [ADR-105 §B](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)** (реализовано в `cbed6ca`): сервис собирается той же функцией, что request-путь (`deps.build_media_generation_service`; до `cbed6ca` согласователь собирал его сам — без `request_logs` и `moderation`); задача, на которую fal не даёт конечного ответа, доводится до `failed` с возвратом не позже `MEDIA_JOB_DEADLINE_SECONDS`; при пустом `FAL_API_KEY` — без опроса. Push по-прежнему только на `completed`. Полное описание — [media-generation/03-architecture.md §Согласователь](../media-generation/03-architecture.md#согласователь-одна-сборка-сервиса-adr-105-b6).

## Инварианты
- Токен принадлежит `sub`; один на `(user, device)`.
- `push_token` минимизируется в логах.
