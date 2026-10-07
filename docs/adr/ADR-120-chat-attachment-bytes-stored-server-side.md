# ADR-120 — Байты вложений чата хранятся на сервере: `chat_attachments`, `attachmentRefs` у всех типов, наследование при редактировании

- **Статус:** Accepted. **Состояние реализации:** docs-only на 2026-10-07; код и миграция — зона `backend`.
- **Дата:** 2026-10-07
- **Меняет:** [ADR-020 §3](ADR-020-inline-base64-attachments-mvp.md) (байты вложения больше не «живут только в первом обращении к модели»), [ADR-071 §2](ADR-071-chat-attachment-refs-and-history-pagination.md) (`attachmentRefs` — у всех типов, `url` — наш подписанный адрес), [ADR-088 §1 п.5](ADR-088-attachments-per-turn-contract.md) (вложения при редактировании наследуются).
- **Связано:** [ADR-090](ADR-090-chat-documents.md) (образец таблицы и каскада), [ADR-085](ADR-085-media-asset-download-proxy.md) (подписанный URL), [ADR-040](ADR-040-edit-message-and-regenerate.md) (усечение истории), [ADR-086 §3](ADR-086-ugc-moderation.md) (модерация входа чата), [TD-009](../100-known-tech-debt.md), [TD-015](../100-known-tech-debt.md).
- **Миграция:** `0042_chat_attachments` (`down_revision = "0041_media_jobs_remaining_routes"`), expand-only, без backfill.

## Контекст

Байты вложения чата (фото, PDF, текстовый файл) сервер видит один раз — в запросе хода — и не сохраняет: в `chat_steps.payload` пишется плейсхолдер ([ADR-020 §3](ADR-020-inline-base64-attachments-mvp.md)), у фото дополнительно — ссылка fal на 24 ч (`payload.attachmentRefs`, [ADR-071 §2](ADR-071-chat-attachment-refs-and-history-pagination.md), только при настроенной медиа-генерации). Следствия, на которые жалуется клиент:

1. История чата на другом устройстве или после переустановки не показывает ни PDF/TXT, ни (через сутки) фото.
2. «Изменить сообщение» и «Перегенерировать» (`editMessageStepId`) теряют вложение: ход собирается только из присланного ([ADR-088 §1 п.5](ADR-088-attachments-per-turn-contract.md)), а клиент байтов уже не имеет.

## Решение

### 1. Хранилище — таблица `chat_attachments` (BYTEA) в БД инстанса

| Колонка | Тип | Ограничения | Смысл |
|---|---|---|---|
| `id` | `uuid` | PK, `gen_random_uuid()` | `attachmentId` |
| `user_id` | `uuid` | FK `users.id` `ON DELETE CASCADE`, NOT NULL | владелец |
| `session_id` | `uuid` | FK `chat_sessions.id` `ON DELETE CASCADE`, NOT NULL | чат |
| `message_step_id` | `uuid` | NOT NULL | ход, в котором вложение прислано (`chat_steps.message_step_id` user-шага); FK нет — значение не уникально в `chat_steps` |
| `position` | `smallint` | NOT NULL | порядковый номер в `attachments[]` запроса |
| `type` | `text` | NOT NULL | `image` \| `document` \| `text` |
| `media_type` | `text` | NOT NULL | из allowlist `AttachmentMediaType` |
| `filename` | `text` | NOT NULL | присланное `filename` или `file` (дефолт `_placeholder`, `src/app/chat/attachments.py`) |
| `size_bytes` | `integer` | NOT NULL | длина декодированных байтов |
| `content` | `bytea` | NOT NULL | декодированные байты |
| `created_at` | `timestamptz` | NOT NULL, default `now()` | |

Уникальный индекс `ux_chat_attachments_turn (session_id, message_step_id, position)` — им же выбираются вложения хода.

- **Что хранится:** каждое вложение классов `image`, `document`, `text`, прошедшее `prepare_attachments`. **`audio` не хранится:** запись распознаётся и заменяется текстом до сборки хода ([ADR-095](ADR-095-voice-messages.md)), исходник не показывается.
- **Момент записи:** в той же транзакции, что `add_step` user-шага хода (`ChatOrchestrator`, место записи `user_payload`). Отказ хода до коммита откатывает и вложения.
- **Удаление:** удаление чата (`ChatsRepository.delete_session`) и пользователя — каскадом FK. Усечение истории (`ChatRepository.truncate_from_message_step`, [ADR-040](ADR-040-edit-message-and-regenerate.md)) удаляет строки `chat_attachments` всех усечённых ходов **явно**, в той же транзакции (FK к шагу нет).
- **Лимиты:** без изменений — `ATTACHMENT_MAX_COUNT`, `ATTACHMENT_MAX_BYTES_IMAGE`/`_DOCUMENT`, `ATTACHMENT_TOTAL_BYTES` ([ADR-089](ADR-089-attachment-limits-and-error-taxonomy.md)). Квоты на объём хранения нет: объём ограничен лимитом хода и временем жизни чата; рост БД и бэкапов — [TD-009](../100-known-tech-debt.md).
- **Инвариант [ADR-020 §3](ADR-020-inline-base64-attachments-mvp.md) сохраняется:** `chat_steps.payload` по-прежнему не содержит base64; реплей истории модели не меняется — прежние вложения модели на следующих ходах не пересылаются ([ADR-088 §1 п.3–4](ADR-088-attachments-per-turn-contract.md)).

### 2. `payload.attachmentRefs` — у всех хранимых вложений, аддитивно

**Хранимая форма** (user-шаг, порядок = `position`):

```jsonc
{"attachmentId":"<uuid>","mediaType":"image/jpeg","filename":"photo.jpg","size":240123,
 "url":"https://v3.fal.media/...","expiresAt":"2026-10-08T10:00:00Z"}   // url/expiresAt — только фото с успешной заливкой в fal, как сегодня
```

**Отдаваемая форма** (`GET /v1/chats/{id}`, нормализация на чтении `ChatsService._normalize_payload`, deep copy): для записи с `attachmentId` поля `url` и `expiresAt` **заменяются** нашим подписанным адресом (§3) и моментом истечения подписи; подпись строится на каждый GET. Записи без `attachmentId` (шаги до миграции) отдаются как хранятся. `PREVIEW_URL_SECRET` не задан → `url`/`expiresAt` у записи отсутствуют, WARNING `chat_attachment_url_secret_missing`.

Совместимость `useRecentImage`: клиент по-прежнему видит у фото `https`-`url` и `expiresAt` в будущем. Сервер для медиа-генерации читает **хранимый** fal-`url` (`latest_alive_image_urls`, `src/app/chat/attachment_refs.py`); если живого нет — ветка `useRecentImage` в `GlobalToolHandlers._media_ask_params` и `GlobalToolHandlers._media_generate` (там, где сегодня возвращается `no_recent_image`) берёт последнее `image` из `chat_attachments` этой сессии и заливает его в fal тем же `GlobalToolHandlers._upload_turn_images`. **Лениво — только при вызове инструмента с `useRecentImage: true`**; `ChatOrchestrator._recent_image_urls_for_session` вызывается на каждом ходе и заливку не делает. Мягкая ошибка `no_recent_image` остаётся для сессий без хранимого фото.

### 3. Отдача — `GET /v1/chats/{sessionId}/attachments/{attachmentId}/{token}`, подписанный URL

- **Подпись, а не JWT:** фото в истории грузит загрузчик изображений клиента без заголовка авторизации — тот же довод, что у медиа-ассетов ([ADR-085](ADR-085-media-asset-download-proxy.md)); правило «по JWT» [ADR-090 §4](ADR-090-chat-documents.md) относится к документам чата и сюда не переносится.
- Токен: `base64url(exp).base64url(HMAC_SHA256(PREVIEW_URL_SECRET, "chat-attachment|{attachmentId}|{sessionId}|{ownerUserId}|{exp}"))`, проверка constant-time. Префикс `chat-attachment|` не даёт токену медиа или превью открыть вложение и наоборот. Модуль — по образцу `src/app/media_generation/signed_url.py`.
- TTL — новая `CHAT_ATTACHMENT_URL_TTL_SECONDS` (дефолт `86400`; `<= 0` → дефолт). Адрес абсолютный, на `SERVICE_DOMAIN` ([ADR-031](ADR-031-absolute-preview-url.md)).
- Ответ `200`: тело — `content`, `Content-Type: <media_type>` (для `text/*` и `application/json` — `; charset=utf-8`), `Content-Disposition: inline; filename*=UTF-8''<filename>`, `Content-Length`, `Cache-Control: private, max-age=<остаток подписи>`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox`. `Range` не поддерживается (предел файла 20 MiB).
- Неверный/истёкший токен, чужая или удалённая строка, несовпадение `sessionId` → `404` (существование не раскрывается).

### 4. Редактирование и «Перегенерировать» — наследование вложений исходного хода

Запрос `run` (`/v1/chat/run`, `/v1/chat/v2/run`, `/v1/chat/v2/run/stream`) с `editMessageStepId` и **без вложений** (`attachments` отсутствует, `null` или `[]`) → сервер **до** усечения читает строки `chat_attachments` хода `editMessageStepId` (по `position`), превращает их в `AttachmentIn` (base64 от `content`) и собирает ход тем же путём, что присланные: `prepare_attachments` → блоки модели, плейсхолдеры, новые строки `chat_attachments` под новым `message_step_id`, `attachmentRefs`. **Модерация наследованных вложений не повторяется** — они прошли её в исходном ходе ([ADR-086 §3](ADR-086-ugc-moderation.md)); текст сообщения при наличии новых вложений модерируется как сейчас.

- Есть присланные `attachments[]` → используются только они (замена, не объединение).
- У исходного хода строк нет (шаг до миграции, ход без вложений) → поведение прежнее.
- Удалить вложение при редактировании без замены нельзя: пустой список равен отсутствию. Сознательно — форма, в которой выпущенные сборки шлют «без вложений» (`[]` или отсутствие поля), не проверена, а потеря фото при правке — та самая жалоба.

### 5. `attachments[].attachmentId` в `run` вместо base64 — НЕ вводится

Единственный сценарий повторной подачи без байтов на клиенте — редактирование/перегенерация — закрыт §4 без изменения схемы запроса. Ссылка на вложение из другого хода/чата требует отдельной проверки владения и решения о модерации; потребителя нет. `AttachmentIn` не меняется.

## Альтернативы

- **Файлы на диске инстанса (каталог ADR-109).** Отвергнуто: удаление с чатом/аккаунтом потребовало бы фонового сборщика сирот вместо FK-каскада; диск не входит в бэкапы ([ADR-115](ADR-115-crm-managed-instance-and-server-lifecycle.md)), и восстановление БД давало бы ссылки на отсутствующие файлы.
- **Объектное хранилище (S3).** Отвергнуто: новая инфраструктура и секреты на 45 инстансов ради объёма, который ограничен лимитом хода; путь выноса — [TD-009](../100-known-tech-debt.md).
- **Двухшаговая модель `POST /v1/attachments` ([ADR-014](ADR-014-multimodal-attachments.md), [TD-015](../100-known-tech-debt.md)).** Отвергнуто: меняет протокол клиента; задача решается хранением байтов уже присланного inline.
- **Отдача по JWT.** Отвергнуто — §3.
- **Хранить base64 в `chat_steps.payload`.** Отвергнуто: нарушает инвариант [ADR-020 §3](ADR-020-inline-base64-attachments-mvp.md), раздувает каждую выборку истории.

## Последствия

- Миграция `0042_chat_attachments`: `CREATE TABLE` + индекс, downgrade — `DROP TABLE`. Без backfill: старые вложения не восстановимы.
- Объём БД и WAL-архива растёт на объём вложений (до 60 MiB на ход) — [TD-009](../100-known-tech-debt.md).
- Контракт аддитивен: новые поля `attachmentId`, `size` в `attachmentRefs`; новый GET-маршрут; `url`/`expiresAt` фото теперь наш адрес.
- `AttachmentIn`/`ChatRunRequest.attachments` — меняется только текст `description` (наследование при `editMessageStepId`).

## Тесты

- **Integration** (зона `qa`): запись строк в транзакции хода; каскад при `DELETE /v1/chats/{id}`; удаление строк усечённых ходов; edit без `attachments` → модель получает блоки исходного хода, модерация не вызывается; edit с `attachments` → только новые; `GET` чата → `attachmentRefs[].url` подписан, `attachmentId`/`size` есть у PDF/TXT; download: верный токен → байты и заголовки, чужой/истёкший/подменённый `sessionId` → `404`; `useRecentImage` без живого fal-url → ленивая заливка из БД.
- **Migration** (зона `qa`): `0042` upgrade/downgrade на одноразовой БД.
- Существующие тесты на «вложения при `editMessageStepId` не наследуются» ([ADR-088](ADR-088-attachments-per-turn-contract.md)) и на форму `attachmentRefs` чинит `qa`.
