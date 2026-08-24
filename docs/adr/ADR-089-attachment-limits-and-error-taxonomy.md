# ADR-089 — Лимиты вложений в контракте, `413` без обрыва соединения, разведение кодов ошибок вложений

- **Статус:** Accepted
- **Дата:** 2026-08-24
- **Тип:** контрактный + транспортный ADR; **закрывает** [TD-017](../100-known-tech-debt.md); **уточняет** [ADR-020](ADR-020-inline-base64-attachments-mvp.md) и [ADR-045](ADR-045-per-path-body-limit-workspace-files.md) (тела не переписаны — immutability)
- **Связано:** [ADR-004](ADR-004-blocked-http-200.md) (формат ошибки), [ADR-062](ADR-062-media-upload-via-fal-storage.md) (`/v1/media/uploads`), [ADR-069](ADR-069-sse-text-streaming.md) (SSE-роут), [ADR-086](ADR-086-ugc-moderation.md) (`content_policy_violation` в той же семье кодов), [ADR-088](ADR-088-attachments-per-turn-contract.md)
- **Реализуется в:** [modules/api-gateway](../modules/api-gateway/README.md), [modules/chat-orchestrator](../modules/chat-orchestrator/README.md), [modules/media-generation](../modules/media-generation/README.md)

## Контекст

Багрепорт QA (BUG-004, часть «лимиты и ошибки»):

1. **Лимитов нет в OpenAPI** — их пришлось находить перебором: размер одного вложения, максимум вложений за ход, размер всего тела.
2. **При превышении размера тела соединение рвётся без HTTP-ответа** (broken pipe), и приложение показывает пользователю «нет связи» вместо «файл слишком большой».
3. **Все `422` приходят с одним `code: "validation_error"`**, различается только текст (`attachment exceeds the maximum size`, `too many attachments`, `unsupported_media_type: …`, `PDF could not be parsed`, `request validation failed`) — клиент вынужден разбирать строку.

Разбор причин по коду:

- **(1)** значения лимитов живут только в `src/app/config.py` и в `docs/`; в описаниях полей OpenAPI их нет.
- **(2)** `SizeLimitMiddleware` — `BaseHTTPMiddleware`, который сравнивает **заголовок `Content-Length`** и, при превышении, возвращает `413` **не прочитав тело**. Клиент в этот момент ещё передаёт байты; сервер закрывает соединение → на стороне iOS это `-1005`/broken pipe, а не HTTP-ответ. Плюс уже зафиксированное [TD-017](../100-known-tech-debt.md): при **отсутствии** `Content-Length` (chunked) проверка не выполняется вовсе.
- **(3)** все content-level отказы вложений поднимаются одним классом `ValidationFailedError` с `code = "validation_error"`; различие несёт только `message`.

Отдельно, при сверке карты лимитов с кодом обнаружено: повышенный transport-лимит применяется к множеству `{"/v1/chat/run", "/v1/chat/v2/run"}`, а **`POST /v1/chat/v2/run/stream` в него не входит**, хотя принимает тот же `ChatV2RunRequest` с `attachments[]` ⇒ вложение больше ~512 KB на SSE-роуте отбивается транспортным лимитом, которого в контракте этого роута нет.

## Решение

### 1. Повышенный transport-лимит — правило по инварианту, а не перечень путей

> **Инвариант Л-1.** Повышенный transport-лимит (`ATTACHMENT_REQUEST_BODY_LIMIT`) применяется к **каждому** роуту, тело которого может содержать `attachments[]`. Перечень путей — производная от этого признака, а не сам признак.

Следствие немедленное: в множество добавляется **`/v1/chat/v2/run/stream`**. Прочие `/v1/chat/*` (обе версии `tool-result`, `capabilities`) остаются на общем `SIZE_LIMIT_BODY` — они `attachments[]` не принимают ([ADR-088](ADR-088-attachments-per-turn-contract.md) §1.2).

**Проверяется тестом-детектором, а не глазами:** пройти по роутам приложения, у которых тип тела — `ChatRunRequest` или его подкласс, и убедиться, что `SizeLimitMiddleware._limit_for(path)` возвращает `attachment_request_body_limit` для **каждого** из них. Добавление нового роута с вложениями без правки карты лимитов роняет этот тест.

### 2. `413` вместо обрыва соединения

`SizeLimitMiddleware` переводится с `BaseHTTPMiddleware` на **чистый ASGI-middleware** (`__call__(scope, receive, send)`) и получает две ветки:

- **Ветка A — ранняя, по `Content-Length`.** Заголовок присутствует и больше лимита роута → ответ `413` отправляется сразу.
- **Ветка B — потоковая, по фактически прочитанным байтам** (закрывает [TD-017](../100-known-tech-debt.md)). `receive` оборачивается счётчиком; как только суммарный объём тела превысил лимит роута, приложение к телу больше не допускается и отправляется тот же `413`. Ветка работает и без `Content-Length` (chunked), то есть транспортный guard перестаёт зависеть от заголовка, который контролирует клиент.

**В обеих ветках, прежде чем закрыть соединение:**

1. ответ отдаётся с заголовком `Connection: close`;
2. остаток тела **вычитывается и отбрасывается** (drain) — но не более `SIZE_LIMIT_DRAIN_BYTES` (дефолт `1048576`, 1 MiB) сверх уже прочитанного. Drain нужен ровно затем, чтобы клиент успел дописать запрос и **прочитать ответ**: закрытие сокета на середине аплоада и есть тот broken pipe, который приложение показывает как «нет связи»;
3. если тело не закончилось в пределах drain-бюджета — соединение закрывается. Безлимитный drain означал бы «читать до конца ровно тот гигабайт, ради отклонения которого стоит лимит», то есть отменял бы защиту.

**Тело ответа не меняется** (совместимость): `{"error":{"code":"payload_too_large","message":…,"requestId":…}}`. `message` становится детерминированным и **несёт число**: `request body exceeds the limit of <N> bytes for this route`, где `<N>` — тот же `_limit_for(path)`, что применён при проверке (не второй литерал). Машиночитаемое поле лимита в теле ошибки — [Q-089-1](../99-open-questions.md).

### 3. Разведение кодов ошибок вложений

HTTP-статусы **не меняются** (требование обратной совместимости), `message` каждого отказа сохраняется **дословно** — клиент, который сегодня разбирает строку, продолжает работать. Меняется только `code`:

| Условие | HTTP | `code` (было → стало) | `message` (не меняется) |
|---|---|---|---|
| `len(attachments) > ATTACHMENT_MAX_COUNT` | `422` | `validation_error` → **`too_many_attachments`** | `too many attachments` |
| одно вложение больше класс-лимита | `422` | `validation_error` → **`attachment_too_large`** | `attachment exceeds the maximum size` |
| сумма вложений больше `ATTACHMENT_TOTAL_BYTES` | `422` | `validation_error` → **`attachments_total_too_large`** | `attachments exceed the total size limit` |
| `mediaType` вне allowlist | `422` | `validation_error` → **`unsupported_media_type`** | `unsupported_media_type: <mediaType>` |
| содержимое не соответствует `mediaType` (magic bytes / UTF-8 / JSON) | `422` | `validation_error` → **`attachment_media_type_mismatch`** | `attachment content does not match declared mediaType` / `text attachment is not valid UTF-8` |
| битый base64 | `422` | `validation_error` → **`invalid_base64`** | `attachment data is not valid base64` |
| PDF не парсится / запаролен | `422` | `validation_error` → **`pdf_unreadable`** | `PDF could not be parsed` / `password-protected PDF is not accepted` |
| страниц PDF больше `ATTACHMENT_PDF_MAX_PAGES` | `422` | `validation_error` → **`pdf_too_many_pages`** | `PDF exceeds the maximum allowed number of pages` |
| ошибка схемы запроса (Pydantic/`extra=forbid`/лимит поля) | `422` | `validation_error` (**без изменений**) | `request validation failed` |
| тело больше transport-лимита роута | `413` | `payload_too_large` (**без изменений**) | см. §2 |

> **Forward-compat правило для `error.code` (нормативно).** До этого ADR `code` публиковался как ЗАКРЫТЫЙ набор, поэтому выпущенная сборка, ветвящаяся по `code == "validation_error"`, получит теперь незнакомое значение. Клиент **обязан** трактовать неизвестный `error.code` как generic-ошибку соответствующего HTTP-статуса и не падать — тот же приём, что уже закреплён для `keyStatus` ([ADR-016](ADR-016-byok-key-lifecycle.md)) и для `modality` ([ADR-087](ADR-087-default-chat-model-gpt-4-1.md) §5). Честная оговорка: для клиентов, ветвящихся по `code`, это **изменение наблюдаемого поведения** — HTTP-статус и текст `message` не меняются, но значение `code` меняется. Клиенты, разбирающие `message`, не затронуты.

**Контраст (обе стороны помечены намеренно). Прогон по ВСЕМ поверхностям, принимающим те же байты:**

| Поверхность | Превышение размера сегодня | Новые `code` |
|---|---|---|
| `attachments[]` в `/v1/chat/*` | `422 validation_error` | **да** — разводятся по таблице выше |
| `POST /v1/media/uploads` | `413 payload_too_large` | нет — статус и код сохраняются |
| `POST /v1/workspaces/{id}/files` | `413 payload_too_large` (`PayloadTooLargeError` в `workspaces/text_extract.py`) | нет — статус и код сохраняются |

`code` разводится **только там, где отказ и сегодня `422`**. На двух `413`-поверхностях `payload_too_large` остаётся как есть: смена статуса — прямой breaking change, прямо запрещённый требованием. Асимметрия историческая и **сохраняется**; правило «content-level отказ = 422» на uploads не переносится, правило «per-file cap = 413» на чат не переносится.

**Sweep по всем поверхностям, использующим общие валидаторы вложений** (`_decoded_len_from_base64`, `_decode_base64`, `_check_magic_bytes`, `_check_pdf_pages`): `POST /v1/chat/run`, `POST /v1/chat/v2/run`, `POST /v1/chat/v2/run/stream`, `POST /v1/media/uploads`, `POST /v1/workspaces/{id}/files`. Новые коды поднимаются **из самих валидаторов**, поэтому применяются на всех перечисленных путях одинаково; отдельного «кода для workspace-файлов» не вводится — форма отказа одна, значит и код один.

### 4. Публикация лимитов в OpenAPI — со значениями, вычисленными из того же `Settings`

Описания обязаны нести **действующие числа**, а не прозу:

- поле `attachments` — максимум элементов (`ATTACHMENT_MAX_COUNT`) и суммарный лимит (`ATTACHMENT_TOTAL_BYTES`);
- поле `AttachmentIn.data` — класс-лимиты (`ATTACHMENT_MAX_BYTES_IMAGE` / `ATTACHMENT_MAX_BYTES_DOCUMENT`) и page-guard PDF (`ATTACHMENT_PDF_MAX_PAGES`);
- описание роутов, принимающих вложения, — transport-лимит тела (`ATTACHMENT_REQUEST_BODY_LIMIT`) и `413` как его нарушение;
- `POST /v1/media/uploads` — `MEDIA_UPLOAD_MAX_BYTES` / `MEDIA_UPLOAD_REQUEST_BODY_LIMIT` (уже описаны в модульном ТЗ, теперь и в OpenAPI);
- `POST /v1/workspaces/{id}/files` — `WORKSPACE_FILE_MAX_BYTES` / `WORKSPACE_FILES_TOTAL_BYTES` / `WORKSPACE_FILE_MAX_COUNT` / `WORKSPACE_REQUEST_BODY_LIMIT`.

**Нормативно: число в описании обязано вычисляться из того же `Settings`, что и проверка** (подстановка значения при построении схемы), а не быть вторым литералом в тексте. Литерал разойдётся с конфигом на первой же операторской калибровке — и документация снова начнёт врать, как в этом багрепорте. То же правило распространяется на список кодов ошибок в описании роута: он строится из тех же констант, что и сами исключения.

## Альтернативы

1. **Оставить один `validation_error`, отдавать причину в `message`.** Отвергнута — это и есть текущее состояние, из-за которого клиент парсит строки; строка не контракт, она меняется при любой правке текста.
2. **Добавить `error.details`/`error.reason` вместо новых `code`.** Отвергнута: меняет форму конверта ошибки, общую для всего API ([ADR-004](ADR-004-blocked-http-200.md)), ради задачи, которую решает существующее поле `code`.
3. **Сменить статус `attachment_too_large` на `413` для симметрии с uploads.** Отвергнута: смена HTTP-статуса — прямой breaking change (прямо запрещён требованием).
4. **Оставить проверку только по `Content-Length`, а broken pipe объявить особенностью клиента.** Отвергнута: пользователь видит «нет связи» вместо «файл слишком большой»; плюс остаётся открытый [TD-017](../100-known-tech-debt.md).
5. **Читать тело целиком, потом отвечать `413`.** Отвергнута: отменяет смысл транспортного лимита (память/трафик тратятся полностью). Компромисс — ограниченный drain (§2.3).
6. **Поднять `SIZE_LIMIT_BODY` глобально, чтобы SSE-роут перестал резаться.** Отвергнута: расширяет поверхность приёма крупного тела на весь API — прямо против [ADR-020](ADR-020-inline-base64-attachments-mvp.md)/[ADR-045](ADR-045-per-path-body-limit-workspace-files.md).

## Последствия

- Клиент получает `413` с человекочитаемой причиной вместо разрыва соединения — исчезает ложное «нет связи».
- [TD-017](../100-known-tech-debt.md) закрывается: транспортный guard считает фактические байты и работает без `Content-Length`.
- `POST /v1/chat/v2/run/stream` начинает принимать вложения того же размера, что и `/v1/chat/v2/run` (сейчас — тихо режется на 512 KB).
- Клиент может ветвиться по `code` без разбора текста; старые клиенты, читающие `message`, не ломаются (тексты сохранены дословно).
- Перечень кодов расширяется в трёх точках чтения одновременно: [api-gateway/02-api-contracts.md](../modules/api-gateway/02-api-contracts.md), [API-REFERENCE.md §3](../API-REFERENCE.md), OpenAPI-описания роутов.
- Тестовое покрытие (спецификация — [api-gateway/09-testing.md](../modules/api-gateway/09-testing.md), [chat-orchestrator/09-testing.md](../modules/chat-orchestrator/09-testing.md)): сквозной тест «тело больше лимита → получен HTTP-ответ `413`, а не разрыв» (тест обязан падать при откате на middleware, отвечающий без drain); тест chunked-запроса без `Content-Length`; тест-детектор карты лимитов (§1); по одному тесту на каждый новый `code`.
