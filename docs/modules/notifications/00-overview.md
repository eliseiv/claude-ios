# Notifications — Overview

## Назначение
Toggle уведомлений в настройках + регистрация устройства для APNs + доставка push при готовности media generation ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)).

## Scope
- `POST /v1/notifications/device-token` — зарегистрировать/обновить APNs device-токен для `(user, device)`.
- `DELETE /v1/notifications/device-token` — удалить токен устройства (отписка / logout).
- Настройка `notificationsEnabled` — через preferences (`PATCH /v1/preferences`).
- Отправка APNs при `media_jobs.status=completed` (payload: `jobId`, `kind`, `mediaUrl`, `aps.mutable-content=1`).
- Фоновый reconciler non-terminal media jobs (чтобы завершение и push случились без poll клиента).

## Out of scope (остаток [TD-011](../../100-known-tech-debt.md))
- Push по другим событиям (чат, биллинг, …).
- In-app notification center / история уведомлений.
- Локализация alert title/body на сервере.

## Бизнес-правила
- BR-NT-1: один токен на `(user_id, device_id)` — upsert при перерегистрации (`ux_push_tokens_user_device`).
- BR-NT-2: `device_id` — body → JWT claim → `X-Device-Id`; отсутствие → `422`. Токен принадлежит `sub`.
- BR-NT-3: перед отправкой — `user_preferences.notifications_enabled`; выключено → skip.
- BR-NT-4: `push_token` — чувствительный идентификатор; не светится в общих логах.
- BR-NT-5: media deep link — `jobId` (+ `kind`); `conversationId` нет (media изолирован от chat).
