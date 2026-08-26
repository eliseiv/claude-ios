# Chat v2 generation modes: что добавлено

Документ описывает новый chat v2 flow для режимов `general`, `research`, `reasoning`, `study_learn`
([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md) — Study & Learn / квиз).

Главная идея: legacy API оставлен изолированным, а новая логика вынесена в отдельный контракт
`/v1/chat/v2/*`.

> ⛔ **Provider-side continuation (`previous_response_id`) ВЫКЛЮЧЕНА — [TD-032](../../100-known-tech-debt.md).**
> `_CONTINUATION_ENABLED: Final = False` (`src/app/chat/openai_responses_client.py:77`), поэтому
> `_usable_previous_response_id` (`:406`) возвращает `None` на **каждом** ходе и **каждый** v2-ход
> отправляет провайдеру **полный локальный реплей истории** — так же, как legacy. Полный реплей
> здесь **не фолбэк, а единственный путь**. `chat_sessions.provider_state` продолжает **писаться**
> (`set_provider_state`), но **не читается ни одним ходом**: колонка поддерживается актуальной на
> случай, когда выключатель будет переведён, и **не делает следующий ход дешевле**. Причины, почему
> цепочку нельзя просто включить, и триггер закрытия — [TD-032](../../100-known-tech-debt.md).
> Ниже по документу разделы, описывающие механику цепочки, читаются как описание **выключенного**
> пути: они верны как устройство, но ни один ход по ним сегодня не идёт.

## Коротко

- `POST /v1/chat/run` - legacy: полный локальный replay истории, фиксированная цена 1 кредит,
  без `generationMode`, без OpenAI Responses API, без `previous_response_id`.
- `POST /v1/chat/v2/run` - новый режимный чат: `generationMode` на каждый ход, mode-specific
  стоимость, OpenAI **Responses API**, Anthropic web search/thinking. Цепочка `previous_response_id`
  **выключена** ([TD-032](../../100-known-tech-debt.md)) — история реплеится локально, как в legacy;
  отличие v2 от legacy сегодня в режимах, цене и knobs, а не в способе подачи контекста.
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
generationMode: Literal["general", "research", "reasoning", "study_learn"] = "general"
temporary: bool = False
```

Режим не фиксируется на сессию. В одном `sessionId` можно сделать ход `research`, следующий ход
`general`, затем `reasoning` или `study_learn`.

`study_learn` ([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) — четвёртое значение
ЭТОЙ оси, а не отдельная ось: ось `dialogMode` в проект не вводится. Полный контракт режима —
[§Режим study_learn](#режим-study_learn-квиз-adr-064) ниже и
[02-api-contracts.md §POST /v1/chat/v2/run](02-api-contracts.md#post-v1chatv2run).

`temporary` — session-fixed при создании (как `model` / `workspaceProjectId`): пишется только
когда создаётся новая сессия; на resume игнорируется. Temporary-сессия скрыта из
`GET /v1/chats`, остаётся доступна по `sessionId` / `GET /v1/chats/{id}` для multi-turn; клиент
удаляет через `DELETE /v1/chats/{id}`. Legacy `/v1/chat/run` поле отвергает (`422`).

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

Зачем: все режимы, новая цена и — когда [TD-032](../../100-known-tech-debt.md) будет закрыт —
provider continuation включаются только здесь. Сегодня из этого списка работают режимы и цена;
цепочка `previous_response_id` выключена, и `generation_backend="v2"` на способ подачи контекста не
влияет.

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

**Состав массива — не «все режимы backend», а объявляемые ЭТИМ инстансом ([ADR-065 §1](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)).** Пример выше — инстанс с дефолтной конфигурацией: `study_learn` **не объявлен**. Гейт — env-allowlist `CHAT_ADVERTISED_GENERATION_MODES` (дефолт `general,research,reasoning`); на инстансе с квиз-UI он перечисляет `study_learn`, и тогда элемент `{"mode": "study_learn", "creditCost": 2, "available": true}` добавляется **четвёртым** (порядок канонический, независимо от порядка в env). `creditCost` берётся тем же методом `chat_generation_credit_cost`.

**Гейт объявления ≠ выключатель режима.** `/v1/chat/v2/run` принимает `study_learn` на любом инстансе; per-instance флаг **включения** режима отклонён и не вводится; ценой режим не выключить (кламп `≤0 → 1`). Поле `available` у присутствующих элементов всегда `true`, producer'а `false` нет — клиент читает гейт как присутствие элемента. Полный контракт — [02-api-contracts.md §capabilities](02-api-contracts.md#generationmodes--гейт-объявления-adr-065).

## Config

Файл: `src/app/config.py`

Добавленные ENV:

```dotenv
CHAT_CREDIT_COST_GENERAL=1
CHAT_CREDIT_COST_RESEARCH=3
CHAT_CREDIT_COST_REASONING=3
CHAT_CREDIT_COST_STUDY_LEARN=2
CHAT_ADVERTISED_GENERATION_MODES=general,research,reasoning
CHAT_REASONING_LEVEL=medium
ANTHROPIC_THINKING_BUDGET_TOKENS=4096
ANTHROPIC_THINKING_DISPLAY=omitted
ANTHROPIC_WEB_SEARCH_TOOL_TYPE=web_search_20260318
```

**Инвариант положительности цен (нормативно).** Все поля `CHAT_CREDIT_COST_*` проходят **один общий** field-валидатор, который клампит значение `≤ 0` к `1`: мис-конфиг env не должен делать генерацию бесплатной. `CHAT_CREDIT_COST_STUDY_LEARN` обязан быть добавлен **в тот же** валидатор (перечень полей), а не защищён отдельно и не оставлен без защиты. Пропуск не даёт ни ошибки старта, ни блокировки: балансовый гейт пропустит, дебит спишет ноль — режим тихо станет бесплатным на инстансе. Проверяется unit-требованием в [09-testing.md](09-testing.md#integration--study--learn-квиз-adr-064); формулировка для операторов — [07-deployment.md §env](../../07-deployment.md#конфигурация-env).

Методы:

- `chat_generation_credit_cost(generation_mode)` - переводит `general/research/reasoning/study_learn`
  в стоимость кредитов. **Единственный мост «режим → сумма списания»**: одно и то же значение
  используется и для pre-generation balance-гейта, и для финального идемпотентного дебита, и для
  `creditCost` в `GET /v1/chat/v2/capabilities`. Второго механизма цены режима нет и не вводится
  ([ADR-064 §9](../../adr/ADR-064-study-learn-quiz-generation-mode.md)).
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

Хранит provider-owned continuation handle OpenAI Responses API:

```json
{
  "provider": "openai",
  "responseId": "resp_...",
  "model": "gpt-5-mini"
}
```

Это не история сообщений и не пользовательский контекст. Это только ссылка на remote-состояние
провайдера.

⛔ **Колонка ПИШЕТСЯ, но НЕ ЧИТАЕТСЯ ни одним ходом** ([TD-032](../../100-known-tech-debt.md)):
`_CONTINUATION_ENABLED` выключен, поэтому handle не уходит провайдеру и **экономии не даёт** —
следующий ход стоит столько же, сколько без него (полный локальный реплей). Значение поддерживается
актуальным на случай, когда выключатель будет переведён. Утверждать, что `provider_state` «делает
следующий ход дешевле», **нельзя**, пока TD-032 открыт.

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

⚠️ `clear_provider_state` (`src/app/chat/repository.py:179`) зовётся **только** из этих трёх мест
(`orchestrator.py:1002`, `:1907`, `:2480`) и **никогда** на upstream-ошибке. Recovery от битого
handle сегодня нет — это одна из четырёх причин, по которым цепочка выключена, и часть работы,
которую описывает [TD-032](../../100-known-tech-debt.md).

### `generation_mode_for_message_step`

Читает `generationMode` из user-step исходного хода.

Зачем: `/v1/chat/v2/tool-result` не принимает `generationMode`, но должен продолжать tool-loop с
тем же режимом и той же ценой, что исходный `/v1/chat/v2/run`.

**Нормативно ([ADR-064 §12](../../adr/ADR-064-study-learn-quiz-generation-mode.md)):** допустимый
набор значений этого чтения — **все четыре** режима (`general`, `research`, `reasoning`,
`study_learn`); неизвестное/отсутствующее значение по-прежнему деградирует к `general`. Если
`study_learn` не входит в набор, continuation квиз-хода **молча** продолжается как `general`: цена
падает до `general` И `quiz.generate` перестаёт предлагаться модели на витках после client-side
tool-вызова. Ошибка тихая (ничего не падает), поэтому покрыта отдельным diff-тестом —
[09-testing.md §Study & Learn](09-testing.md#integration--study--learn-квиз-adr-064).

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
`generation_mode`: legacy path передаёт `general`, либо `research` если на инстансе
`CHAT_LEGACY_WEB_SEARCH_ENABLED` ([ADR-082](../../adr/ADR-082-legacy-web-search.md)).

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

1. ⛔ **Выключено ([TD-032](../../100-known-tech-debt.md)).** По устройству — проверяет
   `provider_state.responseId` и, если state валиден и модель совпадает, отправляет
   `previous_response_id` и только delta после последнего assistant-хода. Фактически
   `_usable_previous_response_id` (`src/app/chat/openai_responses_client.py:406`) возвращает `None`
   на каждом ходе, потому что `_CONTINUATION_ENABLED: Final = False` (`:77`), — так что п. 1-2
   сегодня недостижимы.
2. См. п. 1.
3. **Единственный действующий путь:** собирает full replay из локального `chat_steps`. Это **не
   фолбэк** «когда state не подошёл», а то, как идёт **каждый** v2-ход, пока TD-032 открыт.
4. Для `research` добавляет OpenAI hosted `web_search`. Системный суффикс режима
   (обязать модель искать по теме, а не dummy-запросом) собирает orchestrator, не этот клиент
   ([ADR-084](../../adr/ADR-084-research-system-prompt-suffix.md)).
5. Для `reasoning` передает `reasoning={"effort": ...}`.
6. Парсит `response.id` в `LLMResult.provider_response_id`.
7. Для `study_learn` provider-knobs **не добавляет** — по параметрам вызова это обычная генерация
   (как `general`); отличие режима целиком живёт в tool-наборе и системном промте на стороне
   orchestrator ([ADR-064 §3](../../adr/ADR-064-study-learn-quiz-generation-mode.md)).

Нормализация допустимых режимов внутри клиента (`generation_mode if generation_mode in {…} else
"general"`) обязана содержать **все четыре** значения, включая `study_learn`. Полагаться на то, что
неизвестный режим и так схлопнется в `general`, нельзя: набор — это объявление поддерживаемых
значений, и тихий фолбэк скрыл бы рассинхрон осей при следующем изменении.

Важная деталь: fallback full replay кодирует assistant-текст как Responses input message со строкой
`content`, а не как `output_text` content-part. `output_text` является output-shape, а не обычной
input-shape.

### `AnthropicClient`

Файл: `src/app/chat/anthropic_client.py`

Единственный Anthropic client. Использует Messages API:

- `general` - обычный Messages call;
- `research` - добавляет hosted web-search tool (суффикс промта — на orchestrator, [ADR-084](../../adr/ADR-084-research-system-prompt-suffix.md));
- `reasoning` - передает extended thinking через `extra_body`;
- `study_learn` - обычный Messages call, **без** web-search и **без** thinking (по knobs = `general`);
  отличие режима — в tool-наборе и системном промте, см.
  [§Режим study_learn](#режим-study_learn-квиз-adr-064). Значение обязано входить в whitelist
  допустимых режимов клиента (та же причина, что у Responses-клиента выше).

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
  - следовательно mode-gated инструменты (ось C: `quiz.generate`) на legacy **не предлагаются
    никогда** — не отдельной веткой-исключением, а по построению: гейт считает тот же эффективный
    режим, который здесь принудительно `general`;
  - `provider_state` не читается и не пишется;
  - `usage` не получает `generationMode` и `creditsCharged`.
- `v2`:
  - user-step payload содержит `content` и `generationMode`;
  - стоимость берется из `chat_generation_credit_cost`;
  - LLM получает выбранный режим;
  - tool-набор дополнительно фильтруется **по режиму** (ось C, [ADR-064 §3](../../adr/ADR-064-study-learn-quiz-generation-mode.md)):
    `neutral_tool_definitions(include_server_side=has_project, generation_mode=<эффективный режим>)`.
    Передаётся **тот же** эффективный режим, что уходит провайдеру и в биллинг
    (`_effective_generation_mode`), — одна величина, а не два вычисления;
  - OpenAI credit-mode может читать/писать `provider_state`;
  - `usage` получает `generationMode` и, при debit, `creditsCharged`.

### `_ensure_session_backend`

Проверяет, что session продолжается правильным endpoint-ом.

Правила:

- legacy route не может продолжить v2-сессию;
- v2 `/run` может явно апгрейдить старую/null-сессию в v2;
- v2 `/tool-result` не апгрейдит legacy-сессию, потому что это continuation уже начатого хода.

### `tool_result(..., generation_backend="legacy")`

- legacy continuation: `general` и 1 кредит, либо `research` и `CHAT_CREDIT_COST_RESEARCH`
  при `CHAT_LEGACY_WEB_SEARCH_ENABLED` ([ADR-082](../../adr/ADR-082-legacy-web-search.md));
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

⚠️ **`_provider_state_for_attempt` (`src/app/chat/orchestrator.py:527-545`) сверяет только ИМЯ
провайдера, а не слот ключа.** State, выпущенный резервным аккаунтом (`OPENAI_API_KEY_BACKUP`,
[ADR-074](../../adr/ADR-074-provider-key-failover.md)), был бы передан кандидату **другого**
аккаунта. Сегодня передаваемый state **инертен** (`_usable_previous_response_id` возвращает `None`
в любом случае); сверка слота — часть работы [TD-032](../../100-known-tech-debt.md). Утверждения
вида «сверяется тот же account path» **неверны** — такой сверки в коде нет.

### `_maybe_update_provider_state`

Сохраняет latest OpenAI `response.id` после успешного v2 credit-mode ответа. При `max_tokens`
сбрасывает state, чтобы следующий ход rebuild-ился из локальной истории. Пока
[TD-032](../../100-known-tech-debt.md) открыт, из локальной истории строится **каждый** ход, и
записанное значение ни на что не влияет.

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
- `reasoning` - 3 кредита;
- `study_learn` - 2 кредита ([ADR-064 §9](../../adr/ADR-064-study-learn-quiz-generation-mode.md):
  ход детерминированно делает ≥2 upstream-вызова и даёт объёмный output, но без hosted web search и
  без thinking-бюджета — дешевле `research`/`reasoning`).

Для trial и BYOK внутренние кредиты по-прежнему не списываются.

## Режим `study_learn` (квиз, ADR-064)

Обучающий режим Study & Learn: ответ несёт **пул вопросов** с вариантами, iOS рендерит карточки,
проверка ответов — на клиенте. Полное решение — [ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md);
wire-контракт запроса/ответа/инструмента — [02-api-contracts.md](02-api-contracts.md#post-v1chatv2run).

### Что добавляется

| Слой | Что |
|---|---|
| `src/app/schemas/chat.py` | 4-е значение `GenerationMode`; модели `QuizQuestionSchema`/`QuizSchema`; поле `ChatResponse.quiz` (nullable, **turn-scoped** — содержимое хода, не дельта вызова); 4-й элемент в `ChatCapabilitiesResponse.generationModes` строится роутером |
| `src/app/chat/tools.py` | `TOOL_QUIZ_GENERATE = "quiz.generate"` в `_ARGS_BY_TOOL`/`GLOBAL_SERVER_SIDE_TOOLS`/`TOOL_DESCRIPTIONS`/`_DOMAIN_TO_ANTHROPIC` (`quiz_generate`); новые реестры `TOOL_GENERATION_MODES` и `ARGS_DEGRADE_TOOLS`; параметр `generation_mode` у `neutral_tool_definitions`/`anthropic_tool_definitions`/`openai_tool_definitions` |
| `src/app/chat/global_tools.py` | ветка `quiz.generate` в `GlobalToolHandlers.execute`: Pydantic-валидация пула → `ToolExecution.ok(<эхо>)` либо `ToolExecution.error("invalid_quiz", …)` |
| `src/app/chat/orchestrator.py` | **turn-scoped** `ChatRunOut.quiz`: аккумулятор вызова в `_generate_loop` (last-wins) + единый фолбэк «аккумулятор пуст и режим хода = `study_learn` → последний валидный quiz-шаг хода», применяемый на ВСЕХ ногах (включая `_render_saved_step`); degrade-ветка `ARGS_DEGRADE_TOOLS` в `_handle_tool_use`; guard `tool_not_available` **до** валидации args; статичный системный суффикс режима |
| `src/app/api_gateway/routers/chat.py` | `_to_response`: `quiz` + подавление `assistantMessage` при непустом `quiz`; `generationModes[]` строится по allowlist объявляемых режимов, а не литеральным списком из четырёх элементов ([ADR-065 §1](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)) |
| `src/app/config.py` | `CHAT_CREDIT_COST_STUDY_LEARN` (дефолт `2`) + ветка в `chat_generation_credit_cost` + **включение нового поля в ТОТ ЖЕ field-валидатор положительности, что у трёх соседних цен** (см. §Config); `CHAT_ADVERTISED_GENERATION_MODES` + резолвер объявляемых режимов (дефолт `general,research,reasoning`; `general` всегда; неизвестные значения → игнор + WARNING), [ADR-065 §1](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md) |
| `src/app/observability/metrics.py` | счётчик исходов `quiz_generate_total{result="ok"\|"invalid_quiz"\|"tool_not_available"}` (образец `site_file_write_total`/`token_purchase_total`), [ADR-065 §3](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md) |
| модуль `chats` (чтение) | read-time strip текстовых блоков assistant-шагов у ходов с непустым квизом — `GET /v1/chats/{id}`, `/steps`, превью ([ADR-065 §2](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md), контракт — [chats/02-api-contracts.md §квиз-ход](../chats/02-api-contracts.md#quiz-strip-adr-065)) |
| `src/app/chat/repository.py` | `generation_mode_for_message_step`: whitelist из 4 значений |

Миграции БД нет: пул хранится как обычный tool-результат в `chat_steps.payload`
([04-data-model.md](04-data-model.md#chat_steps)), режим — в user-шаге.

### Гейтинг инструмента (ось C)

`quiz.generate` предлагается модели **тогда и только тогда**, когда эффективный режим хода =
`study_learn`. Реестр — `TOOL_GENERATION_MODES: dict[str, frozenset[str]]` (`quiz.generate →
{"study_learn"}`); инструменты вне реестра по режиму не гейтятся. Оси складываются по И:
ось A (`project_id`, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)) × ось B
(`assistant_mode`, [Q-012-1](../../99-open-questions.md), не реализована) × ось C (режим). Полная
таблица «инструмент × оси» — [03-architecture.md §Оси гейтинга tool-набора](03-architecture.md#оси-гейтинга-tool-набора-adr-022--adr-026--adr-064).

### Системный промт режима

К base-промту `assistant_mode` добавляется **статичная** EN-строка режима, если режим её
объявляет (перед workspace-инструкциями [ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md),
которые остаются последними). Строка статична → внутри режима prompt-кэш стабилен; у режима со
суффиксом при этом **своя** запись кэша (префикс отличается и суффиксом, и tool-набором) — ожидаемо,
не дефект.

- `study_learn`: задавать вопросы **только** через `quiz.generate`, не повторять формулировки
  вопросов в тексте и не раскрывать правильные варианты/пояснения, сопроводительный текст держать
  коротким ([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)).
- `research` ([ADR-084](../../adr/ADR-084-research-system-prompt-suffix.md)): hosted web-search на
  этом ходе живой; для текущих/sourced фактов вызывать его с запросом по теме пользователя, не
  dummy/`calculator`; после результатов — ответ со ссылками; нельзя утверждать, что интернета нет.
  Вешается на **эффективный** `research` (v2 и legacy с `CHAT_LEGACY_WEB_SEARCH_ENABLED`).
  `tool_choice` не форсируется.

### Degrade вместо 422

Провайдерского strict-режима в этой интеграции нет (Anthropic его не поддерживает, Responses-клиент
шлёт `strict: False`), поэтому нарушение ограничений пула моделью — **ожидаемый** сценарий:
`quiz.generate` включён в `ARGS_DEGRADE_TOOLS`, провал валидации даёт tool-result
`invalid_quiz` и ход продолжается. Контракт ошибок, all-or-nothing и граница
`MAX_SERVER_TOOL_ROUNDS` — [02-api-contracts.md §`quiz.generate`](02-api-contracts.md#quizgenerate--server-side-global-tool-режимный-adr-064)
и [ADR-064 §5](../../adr/ADR-064-study-learn-quiz-generation-mode.md).

### Что НЕ меняется

- legacy `POST /v1/chat/run` / `POST /v1/chat/tool-result` — контракт, цена и tool-набор прежние;
  `quiz` в их ответе всегда `null`;
- оси `mode` (`credits|byok`) и `assistantMode` (`chat|code`);
- `GET /v1/tools` — не параметризуется режимом (полный технический реестр; состав и число —
  [02-api-contracts.md §GET /v1/tools](02-api-contracts.md#get-v1tools--каталог-инструментов-adr-019), [ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md));
- барьер хода, идемпотентность, sync-id, `serverTools[]`, blockReason-набор.

## Проверка поведения

Что проверяют тесты:

- `ChatRunRequest` rejects `generationMode`; `ChatV2RunRequest` accepts it.
- Legacy OpenAI client не использует `.responses`, даже если fake SDK его имеет.
- `OpenAIResponsesClient` **никогда** не отправляет `previous_response_id` и реплеит историю целиком
  валидной Responses input-shape — **включая** случай ВАЛИДНОГО сохранённого state с совпадающей
  моделью ([TD-032](../../100-known-tech-debt.md); regression-guard'ы:
  `tests/unit/test_openai_client.py::test_responses_api_replays_full_history_and_never_chains_stored_response_id`,
  `::test_responses_reasoning_keeps_gpt5_and_still_drops_matching_state`,
  `tests/unit/test_responses_usage_model_adr079.py::test_continuation_switch_is_off`,
  `::test_usable_previous_response_id_is_none_even_for_a_valid_state`,
  `::test_valid_state_does_not_reach_the_wire_and_history_is_replayed_in_full`,
  `::test_streaming_valid_state_does_not_reach_the_wire_either`). Совпадение модели **больше не
  является** тем, что решает: решает явный выключатель.
- `AnthropicClient` добавляет web-search/thinking параметры только при `research/reasoning`.
- `research` ([ADR-084](../../adr/ADR-084-research-system-prompt-suffix.md)): system-prompt хода содержит статичный суффикс только при эффективном `research` (v2 и legacy opt-in); dummy-поиск в промте запрещён; `tool_choice` не форсируется.
- `AnthropicClient` в `general` делает обычный Messages call без v2 knobs.
- `/v1/chat/v2/run` списывает mode-specific credits и позволяет переключать режимы в одной сессии.
- `/v1/chat/v2/tool-result` сохраняет исходный mode/cost всего tool-loop хода.
- `/v1/chat/run` остается legacy: 1 кредит, без v2 usage fields.
- `study_learn` ([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)): `quiz.generate`
  предлагается только в этом режиме и не предлагается на legacy; пул возвращается в `quiz` на
  **каждой** ноге хода (`run`, `tool-result`-continuation, реплей), а не только там, где он сгенерирован;
  `assistantMessage` при непустом `quiz` = `null`; невалидный пул деградирует в `invalid_quiz` без
  `422`; списывается цена `study_learn`; continuation сохраняет режим и цену. Полный перечень
  (включая diff-тесты) — [09-testing.md §Study & Learn](09-testing.md#integration--study--learn-квиз-adr-064).
