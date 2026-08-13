# ADR-074 — Spare API keys and cross-provider failover (232-parity)

- **Статус:** Accepted
- **Дата:** 2026-08-13
- **Связано:** [ADR-033](ADR-033-llm-provider-abstraction.md), [ADR-073](ADR-073-dual-credits-llm-providers.md); эталон — 232-claude-backend ADR-047

## Контекст

Credits-чат ходит в апстрим одним сервисным ключом (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`). Если ключ отозван, организация забанена или кончились деньги, пользователь получает 502, хотя у владельца есть запасной ключ и (часто) живой соседний провайдер.

Нужно: та же цепочка, что в 232. Ограничения:

- **`LLM_PROVIDER` не меняется.** Уже выпущенные iOS-клиенты продолжают слать тот же `model` / не слать его вовсе; каталог без `LLM_PROVIDERS` остаётся одно-провайдерным ([ADR-073](ADR-073-dual-credits-llm-providers.md)).
- **BYOK не участвует** — ключ пользователя не ротируется на сервисный.
- Пустые backup / пустая модель обхода → поведение живых инстансов идентично сегодняшнему.
- Dual-credits (`LLM_PROVIDERS`) **не** включается фактом запасных ключей.

Смена провайдера mid-conversation (232 ADR-029) **не** копируется: `chat_steps` хранит wire-формат провайдера сессии. Обход на одном ходе — аварийный; следующий `/chat/run` снова начинается с primary.

## Решение

### 1. Цепочка ключей

| Слот | Каноническое имя (232) | Принимаемый алиас |
|---|---|---|
| OpenAI primary | `OPENAI_API_KEY` | — |
| OpenAI backup | `OPENAI_API_KEY_BACKUP` | `OPEN_AI_BACK_UP_API_KEY` |
| Anthropic primary | `ANTHROPIC_API_KEY` | — |
| Anthropic backup | `ANTHROPIC_API_KEY_BACKUP` | `ANTHROPIC_FALLBACK_API_KEY` |

Порядок для запроса к OpenAI (`gpt-*` / `credits_provider_for_model` = openai):

1. OpenAI + primary + исходная модель
2. OpenAI + backup + исходная модель
3. Anthropic + primary + `OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL` (если задана)
4. Anthropic + backup + та же модель обхода

Для Claude — зеркально (`ANTHROPIC_CHAT_FALLBACK_OPENAI_MODEL`). Пустая модель обхода = кросс-провайдерного перехода нет. Дубликат ключа не добавляет кандидата. Пустая цепочка → один кандидат с `api_key=None` (честный 401 апстрима).

Состояние «ключ мёртв» **не** запоминается между запросами.

### 2. Повод ротации — только отказ УЧЁТНОЙ ЗАПИСИ

| Повод | OpenAI | Anthropic |
|---|---|---|
| ключ неверен / отозван | 401 | 401 |
| организация заблокирована | 403 | 403 |
| кончились средства | 429 + `insufficient_quota` / `credit_balance_exhausted` | **400** + текст `credit balance is too low` |

**Не** ротируют: обычный 429 rate limit (в теле часто есть слово `billing` — машинный `type`/`code` читается **раньше** текста), ошибка формы, 5xx, сеть, таймаут, `ValidationFailedError`.

**Асимметрия направлений** (как в 232):

- ключ → ключ: только credential failure
- Anthropic → OpenAI: **любой** сбой апстрима, сразу к OpenAI, минуя второй ключ Anthropic
- OpenAI → Anthropic: только credential failure

### 3. Что видит клиент

Модель в БД / в ответе iOS остаётся исходной (session-fixed). Подменяется только значение, уходящее апстриму. Пользователь, запросивший GPT, может получить текст Claude — это и есть аварийный обход. Лог `chat_provider_failover` (`reason`, `from_provider`, `from_key_slot`, `to_provider`, `to_key_slot`); ключи не логируются. Метрику Prometheus не заводим.

`provider_state` (Responses `previous_response_id`) передаётся только OpenAI-кандидату; на Anthropic не уходит.

История `chat_steps` после кросс-обхода может смешать wire-формы. Смягчение: OpenAI-клиент вытаскивает текст из anthropic-блоков. Зафиксировано как ограничение аварийного пути, не как mid-chat switch.

## Не меняется

- Контракт `POST /v1/chat/run` / `/v1/chat/v2/run`.
- `LLM_PROVIDER` и каталог без `LLM_PROVIDERS`.
- BYOK ([ADR-044](ADR-044-multi-provider-byok.md)).
- fal.ai.

## Операции

Пример (OpenAI-инстанс, iOS как раньше, обход на Claude при бане обоих OpenAI-ключей):

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_API_KEY_BACKUP=...          # или OPEN_AI_BACK_UP_API_KEY
ANTHROPIC_API_KEY=...
ANTHROPIC_API_KEY_BACKUP=...       # или ANTHROPIC_FALLBACK_API_KEY
OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL=claude-sonnet-4-5
```

Без `OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL` работает только ротация ключей OpenAI. Код сначала, env на живом инстансе — после выката.

## Последствия

- (+) Забаненный/безденежный ключ не роняет чат, если есть запасной или сосед.
- (+) Живые инстансы без новых env неизменны; iOS не требует релиза.
- (−) Кросс-обход на одном ходе смешивает wire-формат в `chat_steps` (приемлемо для аварии; mid-chat switch по-прежнему нет).
- (−) Молчаливая подмена модели (GPT-запрос → ответ Claude) — решение продукта, не скрытый баг.
