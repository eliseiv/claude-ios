# Chats — Architecture

## Размещение
Внутренний пакет (например `src/app/chats/`): репозиторий (`ChatsRepository`) над `chat_sessions`/`chat_steps`/`tool_calls` + use-cases (list/get/steps/rename/pin/delete) + роутер `/v1/chats/*` в API Gateway.

## Автогенерация title
- При создании сессии (`/chat/run` без `sessionId`) orchestrator (или chats-слой, вызываемый orchestrator) проставляет `chat_sessions.title` = усечённый первый user-message (нормализация whitespace, ≤ N символов, дефолт 60). Источник один — без гонки двойной записи.
- `rename` (PATCH) перезаписывает `title` явным значением.
- Если `title` так и не задан (edge) — список отдаёт `null`, клиент показывает fallback (preview).

## preview и поиск
- `preview` — срез текста последнего `chat_steps` (role∈{user,assistant}) с усечением.
- Поиск `q` — `title ILIKE %q%` OR (текст первого user-step ILIKE %q%). На старте без отдельного поискового индекса; при росте объёма — GIN/полнотекст (TD, не заводится до сигнала по латентности — аналогично TD-002).

## Доменная нормализация payload истории при отдаче (ADR-024)
- `GET /v1/chats/{id}` отдаёт `steps[].payload` в **доменном** виде, а не в сыром wire-виде хранилища ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)). Нормализация — на границе сериализации ответа (chats router/service/repository); хранение `chat_steps.payload` и реплей orchestrator **не меняются**.
- Реализация: на отдачу истории сессии строится карта `provider_tool_use_id → domain tool_call_id` **одним** запросом по `session_id` (без N+1); для каждого `tool_use`/`tool_result`-блока `payload.content[]` — `name` через `to_domain_tool_name` (underscore→dot), `id`/`tool_use_id` (`toolu_...`) → domain `tool_calls.id` по карте. Текстовые блоки и `tool_use.input` не трогаются. Переиспользуются ровно `to_domain_tool_name` (`chat/tools.py`) и таблица `tool_calls` — без параллельного маппинга.
- **Двойная форма tool-результата:** шаг `role="tool"` хранится в кастомной доменной форме `{toolCallId, providerToolUseId, toolName, result|error}` (не wire `tool_result`-блок в `content[]`); `_normalize_payload` для него стрипает `providerToolUseId`. Нормализация ADR-024 покрывает и wire `tool_result`-блок в `content[]` (`_normalize_tool_result_block`, forward-compat — orchestrator его не пишет). На обоих путях provider `toolu_...` наружу не утекает. Детали формы — [chat-orchestrator/04-data-model.md](../chat-orchestrator/04-data-model.md).
- **Провайдер-агностичный вход нормализации ([ADR-058](../../adr/ADR-058-provider-agnostic-assistant-payload-in-chats-reads.md)):** форма хранения `payload.content` assistant-шага задаётся активным провайдером ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md) §3) — `anthropic` пишет доменные блоки, `openai` — одно assistant-**сообщение** `{role, content, tool_calls}`. Перед нормализацией выше `_normalize_payload` прогоняет content через `to_domain_blocks` (`chats/provider_blocks.py`): OpenAI-сообщение → `text` + `tool_use` блоки с **сырыми** `id`/underscore-`name`, которые дальше приводит та же логика ADR-024 → `call_...` наружу не утекает. Anthropic-форма проходит без изменений; хранение по-прежнему не трогается (правка только на чтении → уже накопленные чаты чинятся без миграции). Тот же адаптер применён в `_preview` и steps-view.
- **Инвариант:** имя (dot) и id (domain UUID) в истории == `/chat/run` `toolCall.name`/`toolCall.id` того же вызова == `/v1/tools` `name`; provider `toolu_...`/`call_...` наружу в истории не утекает.

## steps-view
- Агрегирует `chat_steps` + `tool_calls` по `message_step_id` в плоский список «шагов» для UI. `kind` выводится из `role`/наличия `tool_use`/`tool_result`. `summary` — короткое человекочитаемое описание (имя tool / первые слова reasoning), без секретов и raw provider id ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md): наружу только доменные имена). Уже отдаёт доменное dot-`toolName` — согласовано с нормализацией `payload` ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)).

## Инварианты
- Chats — только чтение истории + правка метаданных чата. Не пишет `chat_steps`/`tool_calls` (инвариант orchestrator).
- Все запросы скоупятся `WHERE user_id = :sub`; индекс `ix_sessions_user_pinned_updated` обслуживает список.
