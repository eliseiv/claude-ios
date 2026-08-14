# ADR-075 — Unified instance catalog on GET /v1/models (chat + fal)

- **Статус:** Accepted
- **Дата:** 2026-08-14
- **Связано:** [ADR-034](ADR-034-user-model-selection.md), [ADR-060](ADR-060-media-generation-fal.md), [ADR-073](ADR-073-dual-credits-llm-providers.md)

## Контекст

`GET /v1/models` отдавал только credits-чат (`{id, displayName, default, provider}`). Генерация жила отдельно в `GET /v1/media/models`. Клиенту нужен один список всего, что инстанс умеет обслужить: GPT, Claude и fal photo/video — в форме, близкой к 232 (`name`, `modality`, `variant`, `family`).

Ограничения:

- **Живые iOS-клиенты не ломаются.** Обёртка `{models:[…]}` и поле `displayName` остаются. Новые поля аддитивны.
- **Два leftover-ключа dual не включают** ([ADR-073](ADR-073-dual-credits-llm-providers.md)). Chat-состав по-прежнему `credits_providers()` (`LLM_PROVIDER` + opt-in `LLM_PROVIDERS` с непустым ключом).
- **Fal — по факту ключа.** Непустой `FAL_API_KEY` добавляет photo/video; пустой — чат как раньше.
- **`POST /v1/chat/run` `model` принимает только chat-id.** Fal endpoint в `model` → `422 unsupported_model`. Параметры генерации по-прежнему в `GET /v1/media/models` (короткие id `nano-banana-2`).

Динамический Anthropic-fetch и перечень 232 `CHAT_MODELS_OPENAI` **не** копируются: chat-id остаются allowlist инстанса (`OPENAI_MODELS` / `ANTHROPIC_MODELS`, пусто → дефолт провайдера). В каталог попадают только модели, которые этот инстанс реально обслужит.

## Решение

### 1. Аддитивные поля

Каждый элемент: `id`, `displayName`, `name` (= `displayName`), `default`, `provider` (`openai`|`anthropic`|`fal`), `modality` (`chat`|`photo`|`video`), `variant`, `family`. У chat `variant`/`family` = `null`.

### 2. Состав

1. Chat — `Settings.catalog_models()` (порядок и `default:true` чата как в ADR-034/073; дефолт инстанса первый).
2. Если `FAL_API_KEY` непуст — по одной строке на каждый fal-endpoint реестра [ADR-060](ADR-060-media-generation-fal.md) (`id` = endpoint, например `fal-ai/nano-banana-pro/edit`). Дефолт photo — `fal-ai/nano-banana-pro`. Video `default:false`.

Стандартные Kling, которых нет в реестре, не объявляются.

### 3. Дефолты

Ровно один chat-`default:true` (дефолт инстанса). При включённом fal — ещё один photo-`default:true`. Глобальный «ровно один default на весь массив» снят: дефолт теперь per-modality. Старый клиент, который берёт первый `default:true`, по-прежнему получает chat-дефолт (он первый).

## Не меняется

- Обёртка `{models:[…]}` и `displayName`.
- Валидация `chat.model` по union chat-allowlist.
- `GET /v1/media/models` (короткие id, `modes[]`, цены).
- Инстансы без `LLM_PROVIDERS` и без `FAL_API_KEY`.

## Последствия

- (+) Один эндпоинт показывает, что инстанс реально умеет: только OpenAI, OpenAI+Claude, плюс fal при ключе.
- (+) Старые клиенты игнорируют новые ключи; первый `default:true` — по-прежнему chat.
- (−) Старый селектор чата, который рисует все строки, увидит fal-id; отправка их в `chat.model` даст `422`. Новый клиент фильтрует `modality=chat`.
