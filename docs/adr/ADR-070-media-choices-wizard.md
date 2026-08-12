# ADR-070 — Media choices wizard (`mediaChoices` / `mediaSelection` / `media.ask_params`)

- **Статус:** Accepted
- **Дата:** 2026-08-11
- **Связано:** [ADR-068](ADR-068-media-generate-chat-tools.md), [ADR-060](ADR-060-media-generation-fal.md), [ADR-064](ADR-064-study-learn-quiz-generation-mode.md) (паттерн turn-scoped поля), [ADR-039](ADR-039-optional-message-with-attachments.md)

## Контекст

Генерация фото/видео из чата (ADR-068) требует выбора модели, resolution, duration и т.д. Свободный текст и «модель сама угадает enum» дают ошибки каталога. Нужен UX как у квиза: пользователь **тапает** готовые варианты. Поле `quiz` для этого не подходит (study_learn, `correctIndex`, анти-спойлер).

## Решение

### 1. Отдельное поле `ChatResponse.mediaChoices`

Аддитивное nullable-поле (не `quiz`):

```json
{
  "selectionId": "<uuid>",
  "kind": "image",
  "step": "model",
  "questions": [
    {
      "id": "model",
      "question": "Choose a model",
      "options": [{
        "value": "nano-banana-2",
        "label": "Nano Banana 2 · from 4 cr.",
        "credits": 4
      }]
    }
  ]
}
```

- Без `correctIndex` / `explanation`. Промпт для fal **не** отдаётся в `mediaChoices` (только во внутреннем wizard state).
- `assistantMessage` **не** глушится.
- Wizard = **один вопрос за ответ** (каскад: model → priced params → aspectRatio).
- Options **только** из серверного каталога (`catalog.py` / тот же источник, что `GET /v1/media/models`). LLM enum’ы не передаёт.
- Priced-шаги (`resolution` / `duration` / `audio`): цена в `options[].credits`, в `label` (`· N cr.`) и дублем в тексте `question` (`1K: 4 cr., 2K: 6 cr., …`).

### 2. Tool `media.ask_params`

Global server-side, не mode-gated. Args: `{ kind: image|video, prompt, sourceJobId? }`.  
Execution: создать `selectionId`, первый шаг (`model`), persist как tool-result. Soft degrade при невалидных args.

`media.generate_*` остаются, когда параметры уже известны. System prompt: при неясной модели/качестве — сначала `media.ask_params`.

### 3. Тело `/v1/chat/v2/run`: `mediaSelection`

```json
{ "selectionId": "<uuid>", "answers": { "model": "nano-banana-2", "resolution": "2K" } }
```

- Ветка **до LLM**: merge answers → следующий `mediaChoices` **или** `MediaGenerationService.submit` → `mediaJobs`.
- Пустой `message` допустим, если есть `mediaSelection` (расширение ADR-039 для v2).
- Чужой/неизвестный `selectionId` → `422`.
- Chat-debit за чистый selection-шаг без LLM — **нет**; media debit только на финальном submit.

### 4. История чата (сводка, не N тапов)

- Промежуточные `mediaSelection` **не** создают user/assistant bubbles — answers патчатся в tool-result `media.ask_params`.
- На финале — **один** user-шаг `Media: <kind> · <model> · <params> · <N> cr.` + assistant с текстом и `payload.mediaJobs` (cold start: `jobId` в истории). Текст fal-промпта в историю **не** пишется.
- Labels / `credits` / question title на resolution/duration/audio несут оценку кредитов.

### 5. Edit / image-to-image

- Правки предыдущей генерации: `sourceJobId` (иначе text-to-* заново). System prompt + hint `Most recent media job… sourceJobId=…`.
- Фото **из текущего сообщения** (chat attachment, ADR-020): при `media.ask_params` / `media.generate_*` без `sourceJobId` бэкенд сам заливает attachment на fal (`POST` upload) и кладёт https в `imageUrls` визарда / submit — image-to-image без участия модели в URL. Base64 в `chat_steps` не пишется. Workspace knowledge files не используются как reference.
- Фото из **недавних** user-сообщений: при upload пишется `attachmentRefs` (TTL 1 день, [ADR-071](ADR-071-chat-attachment-refs-and-history-pagination.md)); перед генерацией без нового attach модель **спрашивает**; после «да» — `useRecentImage: true`.

### 6. Не меняется

- Контракт `/v1/media/*`, поле `quiz`, биллинг `media-gen:{jobId}`.

## Последствия

- `GET /v1/tools` +1 (`media.ask_params`).
- iOS: при `mediaChoices` — карточки как квиз; тап → `mediaSelection` на `/v2/run` (или stream); затем `mediaJobs` как сейчас.
- Без миграции БД (состояние в `chat_steps.payload`).
