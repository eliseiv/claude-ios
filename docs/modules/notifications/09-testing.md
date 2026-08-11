# Notifications — Testing

## Unit
- Резолв `deviceId` (тело → JWT → X-Device-Id → `422` при отсутствии).
- Upsert по `(user_id, device_id)` (перерегистрация обновляет токен, не плодит строки).
- APNs payload shape (`mutable-content`, `jobId`, `kind`, `mediaUrl`).
- Push skip: `notifications_enabled=false` / no token / APNs not configured.
- `push_sent_at` claim — второй вызов не шлёт повторно.

## Integration
- `POST /v1/notifications/device-token` — регистрация и повторная (upsert).
- `DELETE` — удаление токена устройства.
- Изоляция: токен другого `sub` недоступен.
- Media completed → push invoked once (fake APNs); reconciler advances stuck job.

## Out of scope
- Реальная доставка в Apple APNs (CI — fake client / httpx mock).
