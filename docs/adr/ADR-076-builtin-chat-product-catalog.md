# ADR-076 — Built-in chat product catalog (OpenAI + Anthropic)

- **Статус:** Accepted
- **Дата:** 2026-08-14
- **Связано:** [ADR-034](ADR-034-user-model-selection.md), [ADR-073](ADR-073-dual-credits-llm-providers.md), [ADR-075](ADR-075-unified-instance-models-catalog.md)

## Контекст

[ADR-075](ADR-075-unified-instance-models-catalog.md) оставил chat-состав на env-allowlist: пустой `OPENAI_MODELS` / `ANTHROPIC_MODELS` → одна дефолтная модель. На живых инстансах allowlist пуст (dual — узкий `{gpt-4o}` / `{claude-sonnet-4-5}`), поэтому селектор не показывал продуктовый набор (`gpt-5.1`, `claude-opus-5`, …).

Нужно: эти модели **видны и выбираемы** (`GET /v1/models` + `chat.model`) на каждом инстансе, где провайдер включён. Без правки `.env` на 16 инстансах. Dual с узким allowlist не должен прятать встроенные id.

## Решение

Встроенный каталог в коде (`src/app/chat/product_catalog.py`):

| OpenAI | Anthropic |
|---|---|
| `gpt-5.1` GPT-5.1 | `claude-opus-5` Claude Opus 5 |
| `gpt-5` GPT-5 | `claude-fable-5` Claude Fable 5 |
| `gpt-5-mini` GPT-5 mini | `claude-opus-4-7` Claude Opus 4.7 |
| `gpt-4.1` GPT-4.1 | `claude-sonnet-4-6` Claude Sonnet 4.6 |
| `gpt-4o` GPT-4o (дефолт флота) | `claude-opus-4-6` Claude Opus 4.6 |
| | `claude-haiku-4-5-20251001` Claude Haiku 4.5 |
| | `claude-sonnet-4-5` Claude Sonnet 4.5 (дефолт флота) |

`allowed_models_for(provider)` = дефолт инстанса **первым** ∪ встроенный каталог провайдера ∪ extras из env. Env **добавляет** id и может переопределить `displayName`; встроенные строки **не скрывает**. Пустой/битый JSON → только дефолт + встроенный каталог.

Состав по-прежнему режется `credits_providers()`: leftover-ключ dual не включает. `chat.model` принимает любой id из этого union.

## Не меняется

- Обёртка `{models}` / поля ADR-075.
- Fal-гейт по `FAL_API_KEY`.
- Session-fixed модель; mid-chat switch нет.

## Последствия

- (+) Селектор на OpenAI-инстансе показывает GPT-5.x / 4.1 без правки `.env`; на Anthropic — Opus/Fable/Sonnet 4.6 / Haiku.
- (−) Env-allowlist больше не сужает каталог до одной модели. Чтобы убрать id — правка кода, не `.env`.
