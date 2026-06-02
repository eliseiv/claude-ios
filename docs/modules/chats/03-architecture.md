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

## steps-view
- Агрегирует `chat_steps` + `tool_calls` по `message_step_id` в плоский список «шагов» для UI. `kind` выводится из `role`/наличия `tool_use`/`tool_result`. `summary` — короткое человекочитаемое описание (имя tool / первые слова reasoning), без секретов и raw provider id ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md): наружу только доменные имена).

## Инварианты
- Chats — только чтение истории + правка метаданных чата. Не пишет `chat_steps`/`tool_calls` (инвариант orchestrator).
- Все запросы скоупятся `WHERE user_id = :sub`; индекс `ix_sessions_user_pinned_updated` обслуживает список.
