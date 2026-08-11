# ADR-068 — Chat tools `media.generate_image` / `media.generate_video` + `ChatResponse.mediaJobs`

- **Статус:** Accepted
- **Дата:** 2026-08-11
- **Связано:** [ADR-060](ADR-060-media-generation-fal.md) (очередь fal, биллинг `media-gen:{jobId}`), [ADR-026](ADR-026-global-server-side-tools-and-time-now.md) (global server-side), [ADR-064](ADR-064-study-learn-quiz-generation-mode.md) (паттерн turn-scoped поля ответа), [ADR-067](ADR-067-media-ready-push-and-reconciler.md) (push по готовности)

## Контекст

Пользователь хочет из чата («сделай видео с котами») запускать генерацию фото/видео. Контракт `/v1/media/*` уже есть и **должен остаться рабочим**. Синхронное ожидание fal в tool-loop невозможно (минуты; ADR-060).

Пересмотр границы ADR-060 «медиа мимо chat-оркестратора»: **байты результата по-прежнему не живут в `chat_steps`**; в чат добавляется только **сабмит** через tools + ссылки `jobId` в ответе хода.

## Решение

### 1. Два global server-side tools

- `media.generate_image` / `media.generate_video` в `GLOBAL_SERVER_SIDE_TOOLS`.
- Не mode-gated (ось C не трогает): доступны в `general` / `research` / `reasoning` / `study_learn` и в legacy `/v1/chat/run`.
- Project-independent (как `time.now`): работают в «чистом чате».
- Исполнение = `MediaGenerationService.submit(...)` (тот же путь, что `POST /v1/media/images|videos`).
- Результат tool: `{ jobId, kind, status, model, creditsCharged }` при успехе; при ошибке — soft `ToolExecution.error` (`invalid_media_request` / `insufficient_credits` / `media_not_configured` / `media_upstream_error`), ход чата **не** падает 422/409.
- Args с `extra=forbid`; невалидные args → degrade (`ARGS_DEGRADE_TOOLS`), как у `quiz.generate`.

### 2. `ChatResponse.mediaJobs`

Аддитивное nullable-поле: список `{ jobId, kind, status, model, creditsCharged }` — **содержимое хода** (`messageStepId`), по паттерну `quiz`:

- аккумулятор текущего вызова (append, не last-wins);
- fallback — успешные tool-result шаги `media.generate_*` этого хода.

Клиент опрашивает `GET /v1/media/jobs/{jobId}` и/или опирается на push (ADR-067).

### 3. Биллинг

Два независимых списания:

1. ход чата — как сейчас (`chat` debit / generation mode cost);
2. media — `media-gen:{jobId}` внутри `submit` (refund при `failed` как сегодня).

### 4. Системный промт

Статичная EN-инструкция в обоих assistant modes: при запросе фото/видео вызвать tool, уточнить модель/качество, не утверждать готовность до сигнала приложения.

### 5. Не меняется

- Контракт и поведение `/v1/media/*`.
- Хранение assets в `media_jobs`, не в chat history.
- Каталог моделей / цены — серверные (anti-tamper).

## Последствия

- Каталог `GET /v1/tools` +2 записи (`execution=server`, `mutating=false`).
- Без миграции БД.
- Инстанс без `FAL_API_KEY`: tools предлагаются, вызов → `media_not_configured` (ход выживает).
