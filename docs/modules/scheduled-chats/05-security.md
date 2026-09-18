# Scheduled Chats — Security

## Threat model (кратко)

| Угроза | Мера |
|---|---|
| Запуск чужого чата | JWT `sub` = `user_id`; session ownership на create/patch и **перед run** |
| Молчаливая подмена resume→new | `session_id` без `ON DELETE SET NULL`; нет строки → `failed`/`session_not_found`, не новая сессия |
| Обход биллинга «бесплатный отложенный ход» | списание/policy в момент run, не при POST; `mode` session-fixed на resume |
| Amplification (много задач) | `SCHEDULED_CHAT_MAX_ACTIVE_PER_USER` + rate-limit |
| Prompt injection в логи/метрики | prompt не логировать целиком; error_message ≤ 500, без секретов |
| Двойной запуск на multi-worker | claim = `FOR UPDATE SKIP LOCKED` + `scheduled→running`; re-claim после `started_at` запрещён |
| Stuck `running` после crash | TTL от `coalesce(started_at, claimed_at)` → `failed`/`worker_interrupted` + push |
| Подмена deep-link | push `sessionId` = `resultSessionId ?? planned ?? null`; при null — fallback по `scheduledChatId` |

## AuthN / AuthZ

- CRUD — только Bearer JWT.
- Воркер — внутренний; наружу HTTP без JWT **не** открывается.
- Admin-поверхности нет.

## Секреты

- APNs credentials — существующие `APNS_*`; не дублировать.
- В таблице секретов нет.

## Privacy

- Уважать `notifications_enabled` (ADR-032).
- Push body — generic; полный prompt в APNs **не** кладётся (только deep-link ids).
