# ADR-071 — Chat attachmentRefs (TTL 1d) + history pagination + hide fal prompt

- **Статус:** Accepted
- **Дата:** 2026-08-12
- **Связано:** [ADR-020](ADR-020-inline-attachments.md), [ADR-062](ADR-062-media-upload-via-fal-storage.md), [ADR-070](ADR-070-media-choices-wizard.md), [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md)

## Контекст

1. Промпт для fal ещё попадал в `GET /v1/chats/{id}` (`ask_params.result.prompt`, `mediaWizard.prompt`, `tool_use.input.prompt`).
2. Нужно переиспользовать фото из недавних сообщений без повторной загрузки байтов клиентом — но после хода base64 не хранится.
3. Полная история чата тяжёлая для iOS — нужна курсорная пагинация без ломания старых клиентов.

## Решение

### 1. Strip fal prompt на read-path

`ChatsService._normalize_payload` удаляет/редактирует prompt только в deep copy ответа. Хранение для wizard submit не трогаем. System prompt: не повторять generation prompt в видимом тексте.

### 2. `attachmentRefs` + `useRecentImage`

- `user.payload.attachmentRefs[]` пишется для каждого хранимого вложения любого типа (`attachmentId`, `mediaType`, `filename`, `size`); фото при настроенном media дополнительно заливается на fal (`url`, `expiresAt = now+24h`). Наружу `url`/`expiresAt` записи с `attachmentId` — наш подписанный адрес ([ADR-120 §2–§3](ADR-120-chat-attachment-bytes-stored-server-side.md)).
- Если в последних ~30 user-steps есть живой ref (или image-placeholder) и на текущем ходе нет нового фото — system hint: **спросить** перед генерацией.
- После согласия: `media.ask_params` / `generate_*` с `useRecentImage: true` → сервер подставляет свежий непросроченный URL. Живого fal-URL нет → заливка байтов последнего фото сессии из `chat_attachments` ([ADR-120 §2](ADR-120-chat-attachment-bytes-stored-server-side.md)); нет и его → soft error `no_recent_image`.

### 3. Пагинация `GET /v1/chats/{id}`

- Query `limit` / `cursor`; без `limit` — полный dump.
- Первая страница с `limit` = последние N (`seq DESC` fetch → ответ `seq ASC`); `nextCursor` → старше.
- Курсор: opaque `(seq|id)`. Order list_steps: `seq ASC` (ADR-021).

## Последствия

- iOS: пагинация опциональна; генерации в чате — `payload.mediaJobs`; лента — `/v1/media/jobs`.
- Без миграции БД.
