# ADR-072 — Per-instance gate for media chat tools (`CHAT_MEDIA_TOOLS_ENABLED`)

- **Статус:** Accepted
- **Дата:** 2026-08-12
- **Связано:** [ADR-060](ADR-060-media-generation-fal.md), [ADR-068](ADR-068-media-generate-chat-tools.md), [ADR-070](ADR-070-media-choices-wizard.md), [ADR-017](ADR-017-multi-instance-deploy.md)

## Контекст

`FAL_API_KEY` включает всю media-поверхность: и REST `/v1/media/*`, и chat-tools (`media.ask_params` / `media.generate_*`). На части инстансов (например `ravelumi.shop`) нужна галерея/REST-генерация, но **не** генерация из чата.

## Решение

Env-флаг **`CHAT_MEDIA_TOOLS_ENABLED`** (bool, default **`true`**, per-instance):

| Значение | `/v1/media/*` (при заданном `FAL_API_KEY`) | Chat media tools / `mediaSelection` / media system-prompt |
|----------|--------------------------------------------|-----------------------------------------------------------|
| `true` (дефолт) | работают | предлагаются модели, как сейчас |
| `false` | работают | **не** предлагаются; soft `tool_not_available`; `mediaSelection` → `422` |

Гейты (все обязательны):

1. `neutral_tool_definitions` / `anthropic_tool_definitions` / `openai_tool_definitions` — параметр `include_media_chat_tools` (оркестратор передаёт значение settings).
2. Soft-refuse в tool-loop, если модель всё же вызвала tool из `MEDIA_CHAT_TOOLS`.
3. System prompt: `_MEDIA_GENERATE_INSTRUCTION` и hints (last media job / recent photo) только при `true`.
4. Ветка `mediaSelection` на `/v1/chat/v2/run` при `false` → `422`.

`GET /v1/tools` остаётся полным техническим реестром (как для `quiz.generate` / `site.*`).

## Не меняется

- Контракт `/v1/media/*`, биллинг `media-gen:{jobId}`, каталог моделей.
- Поведение инстансов с дефолтом `true` (обратная совместимость).

## Операции

Пример (REST media без chat-tools):

```bash
FAL_API_KEY=...
CHAT_MEDIA_TOOLS_ENABLED=false
```

## Последствия

- (+) Разделение «ключ fal» и «генерация в чате» без отдельного образа.
- (−) Ещё один per-instance knob в `.env` / checklist.
