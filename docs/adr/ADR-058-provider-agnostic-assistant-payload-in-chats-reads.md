# ADR-058 — Провайдер-агностичное чтение assistant-шага в chats (preview / история / steps-view)

- Статус: Accepted
- Дата: 2026-07-22
- Тип: bugfix-ADR, **расширяет [ADR-024](ADR-024-history-payload-domain-normalization.md)** (нормализация payload на границе сериализации) и закрывает пробел, внесённый [ADR-033 §3](ADR-033-llm-provider-abstraction.md) (провайдер-специфичный `LLMResult.content_blocks`).
- Связано: [ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md) (нормализация блоков на границе персиста), [ADR-008](ADR-008-provider-tool-use-id.md) (сырой provider-id наружу не отдаётся), [ADR-042](ADR-042-hide-context-block-from-user-facing-history.md) (тот же приём — правка только на чтении). Модуль [chats](../modules/chats/README.md).

## Context

**Инцидент (прод broadnova/avelyra/orvianix, 2026-07-22, воспроизведён на живых инстансах): `GET /v1/chats` отдаёт `preview: null` для каждого чата.**

Свежий пользователь, одно сообщение:

```json
GET /v1/chats     → {"title":"Ответь одним словом: тест","preview":null, ...}
GET /v1/chats/{id}→ {"role":"assistant","payload":{"content":[{"role":"assistant","content":"проверка"}]}}
```

Корень — **расхождение формы хранения между провайдерами**. `chat_steps.payload["content"]` хранит то, что вернул клиент активного провайдера в `LLMResult.content_blocks`, и обязано оставаться wire-валидным для его же replay ([ADR-021](ADR-021-deterministic-step-order-and-block-normalization.md), [ADR-033 §3](ADR-033-llm-provider-abstraction.md)):

- `anthropic` — уже доменные блоки: `[{"type":"text",...}, {"type":"tool_use",...}]`;
- `openai` — нормализованное assistant-**сообщение** одним элементом: `[{"role":"assistant","content":"…","tool_calls":[…]}]` (`openai_client.py` §parse).

При этом **все** пользовательские чтения chats специфицированы на доменной форме блоков и ищут `type == "text"` / `type == "tool_use"`:

1. `ChatsRepository._preview` (`_text_from_payload`) → текста не нашлось → `preview: null` — заявленный контракт «срез последнего сообщения» не выполняется;
2. `ChatsService._normalize_payload` → сырое OpenAI-сообщение уходит в ответ как есть: нарушение [ADR-024](ADR-024-history-payload-domain-normalization.md) (клиент получает форму другого провайдера) и, на ходах с инструментами, утечка сырых `call_...` id вопреки [ADR-008](ADR-008-provider-tool-use-id.md) — маппинг в доменный `tool_calls.id` не срабатывает, потому что блок `tool_use` не распознан;
3. `ChatsService._render_step` / `_text_summary` → `GET /v1/chats/{id}/steps` не отдаёт ни summary ассистента, ни записи `tool_call`.

**Почему не поймали тестами.** Интеграционные тесты гоняют `FakeAnthropicClient`, т.е. только доменную форму; `test_hide_context_block_adr042.py` даже содержит `assert preview is not None` — и он зелёный. Ни один тест не подаёт на чтение OpenAI-форму шага. Дефект чисто прод-специфичный и проявляется на **каждом** OpenAI-инстансе (на 2026-07-22 — все три).

## Decision

**Адаптировать форму на границе сериализации (чтение), а не менять форму хранения.**

Новый чистый модуль `src/app/chats/provider_blocks.py`:

```python
def to_domain_blocks(content: Any) -> list[Any]:
    """Anthropic-форму вернуть как есть; OpenAI assistant-сообщение → доменные блоки."""
```

- Распознавание однозначное и не пересекается: доменный блок дискриминируется по `type` и не имеет `role`; наше OpenAI-сообщение — зеркально (`role == "assistant"`, без `type`), и это ровно один элемент списка.
- Маппинг: `content` (строка) → `{"type":"text","text":…}`; каждый `tool_calls[]` → `{"type":"tool_use","id":<сырой call_…>,"name":<underscore-имя>,"input":<распарсенные arguments>}`.
- `id`/`name` намеренно остаются **сырыми provider-значениями** — ровно в том виде, в каком на этом месте лежит anthropic-блок. Ниже по потоку их приводит к доменным значениям уже существующая нормализация [ADR-024](ADR-024-history-payload-domain-normalization.md) (`_normalize_tool_use_block`), поэтому [ADR-008](ADR-008-provider-tool-use-id.md) выполняется без дублирования логики.
- Путь только на чтение → **никогда не бросает**: не-список → `[]`, невалидный JSON в `function.arguments` → `input = {}`, чужая/будущая форма → возвращается как есть.

Точки применения — три и только три чтения: `ChatsRepository._text_from_payload` (preview), `ChatsService._normalize_payload` (история, на deep-copy) и `ChatsService._render_step` / `_text_summary` (steps-view).

Обоснование выбора:

- **Чинит уже накопленные чаты.** Дефект в данных, записанных за всё время работы OpenAI-инстансов; правка на чтении делает их корректными без миграции.
- **Не трогает генерацию.** Форма хранения остаётся wire-валидной для `_build_provider_messages`; replay, tool-continuation и биллинг не меняются — нулевой риск для рабочего пути.
- **Тот же приём, что уже принят в проекте.** [ADR-024](ADR-024-history-payload-domain-normalization.md) и [ADR-042](ADR-042-hide-context-block-from-user-facing-history.md) правят исключительно границу сериализации на deep-copy; ADR-058 встраивается в этот же слой, а не заводит второй.

**Альтернатива — писать доменные блоки в `chat_steps.payload` и на OpenAI — отвергнута.** Она (а) не чинит существующие чаты без миграции; (б) ломает replay: `_assistant_message_from_blocks` восстанавливает OpenAI-сообщение из персиста вербатим, а его фолбэк `_anthropic_blocks_to_openai_content` **теряет `tool_calls`** (возвращает `[]`) — продолжение хода с инструментами сломалось бы; (в) рискует рабочим путём генерации ради дефекта чтения.

**Границы (что ADR не делает):**

- Не меняет контракт ответов: `preview`, `ChatStepSchema.payload` и steps-view остаются как специфицированы — меняется только то, что раньше деградировало в `null`/сырую форму.
- Не трогает шаги `role="tool"` — их payload провайдер-агностичен по конструкции (`toolCallId`/`toolName`/`result`).
- Не трогает user-шаги — они всегда пишутся доменными text-блоками + плейсхолдерами ([ADR-020 §3](ADR-020-inline-base64-attachments-mvp.md)).
- Не унифицирует форму хранения между провайдерами — это остаётся сознательным следствием [ADR-033 §3](ADR-033-llm-provider-abstraction.md).

## Consequences

- (+) `GET /v1/chats` снова отдаёт `preview` на всех инстансах, включая чаты, созданные до фикса.
- (+) `GET /v1/chats/{id}` соблюдает [ADR-024](ADR-024-history-payload-domain-normalization.md) и [ADR-008](ADR-008-provider-tool-use-id.md) независимо от провайдера; сырой `call_...` наружу не уходит.
- (+) `GET /v1/chats/{id}/steps` показывает summary ассистента и вызовы инструментов на OpenAI-инстансах.
- (−) Знание об OpenAI-форме появляется вне `openai_client` — в одном изолированном лист-модуле. Принято сознательно: альтернатива хуже (см. выше). При добавлении третьего провайдера его форму персиста нужно учесть в `to_domain_blocks` — зафиксировано в docstring модуля.
- Без миграции, без env, контракт API не меняется.

## Тесты

- `tests/unit/test_provider_blocks_adr058.py` — чистый адаптер: обе формы, tool_calls, битые `arguments`, не-список, отсутствие текста.
- `tests/integration/test_openai_assistant_payload_adr058.py` — три эндпоинта на засеянном OpenAI-шаге: `preview` = текст ответа; история = доменные блоки + доменный `tool_calls.id` (и отсутствие `call_...` в теле); steps-view = `reasoning` + `tool_call` с точечным именем; регрессия на anthropic-форме.
