# Chat v2 generation modes: что добавлено

Документ описывает новый chat v2 flow для режимов `general`, `research`, `reasoning`.

Главная идея: legacy API оставлен изолированным, а новая логика вынесена в отдельный контракт
`/v1/chat/v2/*`.

## Коротко

- `POST /v1/chat/run` - legacy: полный локальный replay истории, фиксированная цена 1 кредит,
  без `generationMode`, без OpenAI Responses API, без `previous_response_id`.
- `POST /v1/chat/v2/run` - новый режимный чат: `generationMode` на каждый ход, mode-specific
  стоимость, OpenAI Responses API с `previous_response_id`, Anthropic web search/thinking.
- `POST /v1/chat/tool-result` - legacy continuation.
- `POST /v1/chat/v2/tool-result` - v2 continuation; режим берется из исходного user-step.
- `GET /v1/chat/v2/capabilities` - список режимов и их цена для UI.

`mode=credits|byok` по-прежнему отвечает за способ оплаты. `assistantMode=chat|code` по-прежнему
отвечает за тип ассистента. `generationMode` отвечает только за LLM-возможности конкретного хода.

## API layer

### `ChatRunRequest`

Файл: `src/app/schemas/chat.py`

Legacy request для `/v1/chat/run`. В нем больше нет `generationMode`.

Зачем: старый endpoint остается прежним контрактом. Клиент, который хочет reasoning/research,
должен явно перейти на `/v1/chat/v2/run`.

### `ChatV2RunRequest`

Файл: `src/app/schemas/chat.py`

Новый request для `/v1/chat/v2/run`, наследует обычные поля чата и добавляет:

```python
generationMode: Literal["general", "research", "reasoning"] = "general"
```

Режим не фиксируется на сессию. В одном `sessionId` можно сделать ход `research`, следующий ход
`general`, затем `reasoning`.

### `chat_run`

Файл: `src/app/api_gateway/routers/chat.py`

Handler для `POST /v1/chat/run`.

Вызывает:

```python
orchestrator.run(..., generation_backend="legacy")
```

Зачем: legacy route принудительно идет через старый backend contract.

### `chat_v2_run`

Файл: `src/app/api_gateway/routers/chat.py`

Handler для `POST /v1/chat/v2/run`.

Вызывает:

```python
orchestrator.run(
    ...,
    generation_mode=body.generationMode,
    generation_backend="v2",
)
```

Зачем: все режимы, новая цена и provider continuation включаются только здесь.

### `chat_tool_result` и `chat_v2_tool_result`

Файл: `src/app/api_gateway/routers/chat.py`

- `chat_tool_result` вызывает `orchestrator.tool_result(..., generation_backend="legacy")`.
- `chat_v2_tool_result` вызывает `orchestrator.tool_result(..., generation_backend="v2")`.

Зачем: tool-loop нельзя начинать одним контрактом и продолжать другим. Для v2 continuation
`generationMode` не передается в body, он читается из user-step исходного хода.

### `chat_v2_capabilities`

Файл: `src/app/api_gateway/routers/chat.py`

Endpoint:

```http
GET /v1/chat/v2/capabilities
```

Возвращает активного provider-а, список режимов и стоимость:

```json
{
  "provider": "anthropic",
  "defaultGenerationMode": "general",
  "generationModes": [
    {"mode": "general", "creditCost": 1, "available": true},
    {"mode": "research", "creditCost": 3, "available": true},
    {"mode": "reasoning", "creditCost": 3, "available": true}
  ],
  "reasoningLevel": "medium"
}
```

## Config

Файл: `src/app/config.py`

Добавленные ENV:

```dotenv
CHAT_CREDIT_COST_GENERAL=1
CHAT_CREDIT_COST_RESEARCH=3
CHAT_CREDIT_COST_REASONING=3
CHAT_REASONING_LEVEL=medium
ANTHROPIC_THINKING_BUDGET_TOKENS=4096
ANTHROPIC_THINKING_DISPLAY=omitted
ANTHROPIC_WEB_SEARCH_TOOL_TYPE=web_search_20260318
```

Методы:

- `chat_generation_credit_cost(generation_mode)` - переводит `general/research/reasoning` в
  стоимость кредитов.
- `resolved_reasoning_level()` - нормализует OpenAI reasoning effort.
- `resolved_anthropic_thinking_display()` - нормализует Anthropic thinking display.

Legacy `/v1/chat/run` эти цены не использует и всегда проверяет/списывает 1 кредит. V2 использует
`chat_generation_credit_cost`.

## Data model

### `ChatSession.provider_state`

Файл: `src/app/models/tables.py`

```python
provider_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
```

Хранит provider-owned continuation handle. Сейчас используется для OpenAI Responses API:

```json
{
  "provider": "openai",
  "responseId": "resp_...",
  "model": "gpt-5-mini"
}
```

Это не история сообщений и не пользовательский контекст. Это только ссылка на remote-состояние
провайдера.

### `ChatSession.generation_backend`

Файл: `src/app/models/tables.py`

```python
generation_backend: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Значения:

- `NULL` или `"legacy"` - старая сессия `/v1/chat/*`;
- `"v2"` - сессия нового контракта `/v1/chat/v2/*`.

Зачем: защитить от случайного смешивания legacy/v2 continuation и разных правил billing/state.

### Миграция

Файл: `migrations/versions/20260719_0016_chat_provider_state.py`

Добавляет:

- `chat_sessions.provider_state JSONB NULL`;
- `chat_sessions.generation_backend TEXT NULL`.

## Repository

Файл: `src/app/chat/repository.py`

### `get_or_create_session(..., generation_backend=None)`

При создании новой сессии пишет backend contract: `legacy` или `v2`. На resume не переписывает
поле автоматически.

### `set_generation_backend`

Единая точка записи `chat_sessions.generation_backend`.

Используется при явном переходе старой/null-сессии в v2 через `/v1/chat/v2/run`.

### `set_provider_state` / `clear_provider_state`

Единая точка записи и сброса `chat_sessions.provider_state`.

Сброс нужен при edit/regenerate, max_tokens truncation и при апгрейде legacy-сессии в v2.

### `generation_mode_for_message_step`

Читает `generationMode` из user-step исходного хода.

Зачем: `/v1/chat/v2/tool-result` не принимает `generationMode`, но должен продолжать tool-loop с
тем же режимом и той же ценой, что исходный `/v1/chat/v2/run`.

## LLM factories

Файл: `src/app/chat/llm_client.py`

### Legacy factories

- `get_llm_client()`
- `llm_client_for(provider)`

Возвращают старые клиенты:

- Anthropic -> `AnthropicClient`;
- OpenAI -> `OpenAIClient`.

### V2 factories

- `get_generation_llm_client()`
- `generation_llm_client_for(provider)`

Возвращают generation-aware клиенты:

- Anthropic -> тот же `AnthropicClient`;
- OpenAI -> `OpenAIResponsesClient`.

Зачем: OpenAI v2 отделен от legacy из-за другого API (`Responses`). Anthropic v2 использует тот
же Messages API, поэтому отдельный класс не нужен: режим включается обычным параметром
`generation_mode`, который legacy path передает как `general`.

## Provider clients

### `OpenAIClient`

Файл: `src/app/chat/openai_client.py`

Legacy OpenAI client. Всегда использует:

```python
client.chat.completions.create(...)
```

`generation_mode` и `provider_state` принимает только ради общего `LLMClient` protocol, но
игнорирует их.

### `OpenAIResponsesClient`

Файл: `src/app/chat/openai_responses_client.py`

V2 OpenAI client. Всегда использует:

```python
client.responses.create(...)
```

Что делает:

1. Проверяет `provider_state.responseId`.
2. Если state валиден и модель совпадает, отправляет `previous_response_id` и только delta после
   последнего assistant-хода.
3. Если state отсутствует/модель не совпала, собирает full replay из локального `chat_steps`.
4. Для `research` добавляет OpenAI hosted `web_search`.
5. Для `reasoning` передает `reasoning={"effort": ...}`.
6. Парсит `response.id` в `LLMResult.provider_response_id`.

Важная деталь: fallback full replay кодирует assistant-текст как Responses input message со строкой
`content`, а не как `output_text` content-part. `output_text` является output-shape, а не обычной
input-shape.

### `AnthropicClient`

Файл: `src/app/chat/anthropic_client.py`

Единственный Anthropic client. Использует Messages API:

- `general` - обычный Messages call;
- `research` - добавляет hosted web-search tool;
- `reasoning` - передает extended thinking через `extra_body`.

Legacy `/v1/chat/*` не ломается, потому что orchestrator на legacy path передает только
`generation_mode="general"`. Anthropic в этой интеграции не использует `provider_state`: контекст
продолжает собираться из локальной истории плюс prompt caching.

## Orchestrator

Файл: `src/app/chat/orchestrator.py`

### `run(..., generation_backend="legacy")`

Один метод обслуживает оба публичных контракта, но поведение выбирается явно:

- `legacy`:
  - user-step payload содержит только `content`;
  - стоимость всегда 1 кредит;
  - в LLM отправляется `generation_mode="general"`;
  - `provider_state` не читается и не пишется;
  - `usage` не получает `generationMode` и `creditsCharged`.
- `v2`:
  - user-step payload содержит `content` и `generationMode`;
  - стоимость берется из `chat_generation_credit_cost`;
  - LLM получает выбранный режим;
  - OpenAI credit-mode может читать/писать `provider_state`;
  - `usage` получает `generationMode` и, при debit, `creditsCharged`.

### `_ensure_session_backend`

Проверяет, что session продолжается правильным endpoint-ом.

Правила:

- legacy route не может продолжить v2-сессию;
- v2 `/run` может явно апгрейдить старую/null-сессию в v2;
- v2 `/tool-result` не апгрейдит legacy-сессию, потому что это continuation уже начатого хода.

### `tool_result(..., generation_backend="legacy")`

- legacy continuation всегда `general`, 1 кредит;
- v2 continuation читает исходный `generationMode` через
  `generation_mode_for_message_step(...)` и списывает цену этого режима.

### `_generate_loop`

Выбирает клиент:

- credits + legacy -> injected `get_llm_client()`;
- credits + v2 -> injected `get_generation_llm_client()`;
- BYOK + legacy -> `llm_client_for(byok_provider)`;
- BYOK + v2 -> `generation_llm_client_for(byok_provider)`.

`provider_state` передается только когда:

- backend = `v2`;
- mode = `credits`;
- provider = OpenAI.

BYOK не сохраняет `provider_state`, потому что пользователь может сменить ключ между ходами, а
remote response id привязан к аккаунту/ключу у провайдера.

### `_maybe_update_provider_state`

Сохраняет latest OpenAI `response.id` после успешного v2 credit-mode ответа. При `max_tokens`
сбрасывает state, чтобы следующий ход rebuild-ился из локальной истории.

## Billing

Файлы:

- `src/app/policy/engine.py`
- `src/app/chat/orchestrator.py`
- `src/app/wallet/service.py`

Изменения:

- `evaluate(..., required_credits=1)` теперь умеет проверять баланс против нужной цены.
- `_BillingPlan.credit_amount` хранит сумму debit.
- `_BillingPlan.expose_credit_amount` управляет тем, показывать ли `creditsCharged` в `usage`.
- `_debit(..., generation_mode, amount)` списывает не фиксированную 1, а переданный amount.

Текущие дефолтные цены:

- `general` - 1 кредит;
- `research` - 3 кредита;
- `reasoning` - 3 кредита.

Для trial и BYOK внутренние кредиты по-прежнему не списываются.

## Проверка поведения

Что проверяют тесты:

- `ChatRunRequest` rejects `generationMode`; `ChatV2RunRequest` accepts it.
- Legacy OpenAI client не использует `.responses`, даже если fake SDK его имеет.
- `OpenAIResponsesClient` отправляет `previous_response_id` и delta input.
- `OpenAIResponsesClient` при mismatch модели делает full replay валидной Responses input-shape.
- `AnthropicClient` добавляет web-search/thinking параметры только при `research/reasoning`.
- `AnthropicClient` в `general` делает обычный Messages call без v2 knobs.
- `/v1/chat/v2/run` списывает mode-specific credits и позволяет переключать режимы в одной сессии.
- `/v1/chat/v2/tool-result` сохраняет исходный mode/cost всего tool-loop хода.
- `/v1/chat/run` остается legacy: 1 кредит, без v2 usage fields.
