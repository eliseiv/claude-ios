# ADR-073 — Dual-credits LLM providers (OpenAI + Anthropic) without mid-chat switch

- **Статус:** Accepted
- **Дата:** 2026-08-13
- **Связано:** [ADR-033](ADR-033-llm-provider-abstraction.md), [ADR-034](ADR-034-user-model-selection.md), [ADR-044](ADR-044-multi-provider-byok.md)

## Контекст

На инстансе один сервисный credits-провайдер (`LLM_PROVIDER`, [ADR-033](ADR-033-llm-provider-abstraction.md)). `GET /v1/models` отдаёт только его allowlist; `POST /v1/chat/run` `model` session-fixed ([ADR-034](ADR-034-user-model-selection.md)). Мульти-провайдерность уже есть у BYOK ([ADR-044](ADR-044-multi-provider-byok.md)), но credits-чат на OpenAI-инстансе не может выбрать Claude.

Нужно: на одном инстансе создавать credits-чаты и с GPT, и с Claude. Ограничения:

- **Без смены провайдера внутри чата** (модель остаётся session-fixed; resume игнорирует `model`).
- **Старые эндпоинты и живые инстансы не ломаются.** iOS-клиенты, которые уже ходят в `/v1/models` и `/v1/chat/run`, продолжают работать без релиза приложения.
- Два ключа в `.env` сами по себе dual **не** включают (у OpenAI-клонов часто лежит чужой `ANTHROPIC_API_KEY`).

Смена модели/провайдера mid-conversation ([232-claude-backend] ADR-029) **не** копируется: история `chat_steps` хранит wire-формат провайдера сессии.

## Решение

### 1. Opt-in env `LLM_PROVIDERS`

CSV (`openai,anthropic`). Public, per-instance. **Пусто/не задан → только `LLM_PROVIDER`** — поведение живых инстансов идентично ADR-033/034.

Дополнительный провайдер попадает в credits только если:

- имя `openai` или `anthropic`;
- оно не совпадает с `LLM_PROVIDER`;
- у него непустой API key.

`LLM_PROVIDER` всегда первый (дефолт инстанса). `default:true` в каталоге — по-прежнему `default_model()` активного провайдера.

### 2. `GET /v1/models` — аддитивное поле `provider`

Тот же путь, те же `{id, displayName, default}`. Добавлено поле `provider` (`openai`|`anthropic`). Старые JSONDecoder/Codable **игнорируют неизвестные ключи**. Ровно один `default:true`, он первый. Без `LLM_PROVIDERS` набор id/порядок как раньше, плюс `provider` = `LLM_PROVIDER`.

Создание чата валидирует `model` по **union** allowlist'ов `credits_providers()`. Resume по-прежнему игнорирует поле. Без `model` → дефолт `LLM_PROVIDER` (старые клиенты).

### 3. Маршрутизация credits

`credits_provider_for_model(sess.model)` → `llm_client_for` / `generation_llm_client_for`. Claude-id → Anthropic, GPT-id → OpenAI. Stale-model: id нет среди enabled providers → фолбэк на `LLM_PROVIDER` + `model=None` (как [ADR-044 §Связанное](ADR-044-multi-provider-byok.md)), без 502. Attachments/workspace на credits идут по провайдеру **сессии**, не по `LLM_PROVIDER` (PDF/vision того клиента, который реально вызывается). BYOK не меняется.

Биллинг неизменен (1 кредит / цена режима v2). Миграций нет.

## Не меняется

- Контракт `POST /v1/chat/run` / `/v1/chat/v2/run` (поле `model` то же).
- Session-fixed модель: resume не переключает провайдера.
- Инстансы без `LLM_PROVIDERS`.
- BYOK ([ADR-044](ADR-044-multi-provider-byok.md)).

## Операции

Пример (OpenAI-инстанс + Claude в каталоге):

```bash
LLM_PROVIDER=openai
LLM_PROVIDERS=openai,anthropic
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...          # ключ anthropic-соседа
OPENAI_MODELS={"gpt-4o":"GPT-4o"}
ANTHROPIC_MODELS={"claude-sonnet-4-5":"Claude Sonnet 4.5"}
```

Код сначала, env на живом инстансе — после выката.

## Последствия

- (+) Один инстанс отдаёт оба каталога; старые клиенты продолжают слать `id` в `model`.
- (+) Явный opt-in: два ключа в `.env` dual не включают.
- (−) История чата остаётся wire-форматом провайдера сессии — mid-chat switch по-прежнему невозможен (намеренно).
