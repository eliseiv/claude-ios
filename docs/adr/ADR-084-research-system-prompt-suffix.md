# ADR-084 — Системный суффикс режима `research`

- **Статус:** Accepted
- **Дата:** 2026-08-21
- **Связано:** [ADR-064](ADR-064-study-learn-quiz-generation-mode.md) §7 (прецедент mode suffix), [ADR-082](ADR-082-legacy-web-search.md) (эффективный `research` на legacy), [ADR-036](ADR-036-workspaces-implementation.md) §3 (workspace instructions LAST)
- **Реализуется в:** [modules/chat-orchestrator/10-generation-modes-implementation.md](../modules/chat-orchestrator/10-generation-modes-implementation.md), [09-testing.md](../modules/chat-orchestrator/09-testing.md)

## Контекст

`generationMode=research` уже прикладывает hosted `web_search` провайдера (OpenAI Responses `{type:web_search}`, Anthropic `web_search`) и тарифицирует ход как `CHAT_CREDIT_COST_RESEARCH`. Отдельной research-инструкции в system prompt не было — в отличие от `study_learn`.

Базовый chat-промпт говорит только про device-local tools (`files`, `calendar`, `reminders`). На прод `broadnova.shop` (2026-08-21, `gpt-5.1`, запрос курса USD/RUB) модель вызвала hosted search с dummy-запросом `calculator: 1+1` и ответила, что «нет доступа к интернету». Тул был приложен (`webSearchRequests=1`, 3 кредита), но поиск по сути не состоялся.

`tool_choice` не форсируем: research-ход может не требовать поиска; жёсткий force ломает такие ходы и расходится между провайдерами.

## Решение

К base-промту `assistant_mode` на ходе с **эффективным** режимом `research` добавляется **статичная** EN-строка `_RESEARCH_INSTRUCTION` (тот же слой, что `_STUDY_LEARN_INSTRUCTION`):

- hosted web-search на этом ходе живой и ходит в публичный интернет (это не device-local tool);
- для текущих / датированных / sourced фактов (курсы, новости, законы, цитаты) модель **обязана** вызвать поиск с запросом по теме пользователя;
- dummy / calculator / no-op запросы запрещены;
- после результатов — отвечать по ним и давать рабочие ссылки;
- на Research-ходе нельзя утверждать, что интернета нет или что можно дать только общие советы.

Правила те же, что у суффикса `study_learn`:

1. Суффикс вешается на **эффективный** режим (`_system_prompt_for`). v2 `generationMode=research` и legacy с `CHAT_LEGACY_WEB_SEARCH_ENABLED` (ADR-082) получают его; `general` / `reasoning` / `study_learn` — нет.
2. Строка статична (без даты, счётчиков, содержимого хода) → prompt-кэш внутри режима стабилен; у `research` своя запись кэша (префикс отличается суффиксом и tool-набором) — ожидаемо.
3. Порядок слоёв: base → mode suffix → `workspace.instructions` LAST (ADR-036 §3).
4. Provider knobs, цена, оси гейтинга tool-набора, контракт `/v1/chat/v2/*` **не меняются**.

## Что НЕ меняется

- Приложение hosted `web_search` в клиентах провайдеров.
- Цена `CHAT_CREDIT_COST_RESEARCH` и биллинг «за режим, не за факт вызова».
- Отсутствие `tool_choice` / обязательного вызова поиска на каждом research-ходе.
- `study_learn`-суффикс и anti-spoiler (ADR-064 / ADR-065).
