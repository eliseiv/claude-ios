# Documents — Implementation Phases

## Фаза 1 — ядро ([ADR-090](../../adr/ADR-090-chat-documents.md)) — **РЕАЛИЗОВАНА** (2026-08-24)

1. Миграция **`0027`**: таблица `chat_documents` + индекс `ix_chat_documents_session_created` (expand-only; цепочка ревизий и head в `docs/` не перечисляются — [07-deployment.md §Миграции](../../07-deployment.md#миграции)).
2. `DocumentsService` (`src/app/documents/`): CRUD, потолки на сессию, нормализация имени, строка контекста для промта.
3. Роутер `/v1/chats/{sessionId}/documents` + `/download`, строгие схемы, `Content-Disposition` по RFC 5987.
4. Инструменты `document.create` / `document.list` / `document.read` / `document.update` в `GLOBAL_SERVER_SIDE_TOOLS`; регистрация в `MUTATING_TOOLS` и `ARGS_DEGRADE_TOOLS`; domain↔anthropic маппинг имён.
5. Инъекция строки о документах в системный промт.

## Фаза 2 — `ChatResponse.documents[]` ([ADR-101](../../adr/ADR-101-chat-response-documents.md)) — **спроектирована, код не написан**

Только проекция уже существующего состояния в ответ хода. Доменный слой документов, схемы REST, лимиты, миграции — **не трогаются**.

1. **Схема** (`src/app/schemas/chat.py`): `ChatDocumentRefSchema(StrictModel)` — `documentId: uuid`, `filename: str`, `mediaType: str`, `size: int`, `version: int` (образец — `MediaJobRefSchema`, `:530`). Поле `documents: list[ChatDocumentRefSchema] | None = None` в `ChatResponse` рядом с `mediaJobs` (`:764`).
2. **Транспорт хода** (`src/app/chat/orchestrator.py`): поле `documents: list[dict[str, Any]] | None = None` в `ChatRunOut` (рядом с `media_jobs`, `:843`); аккумулятор по образцу `_MediaJobsAccumulator`, наполняемый успешными результатами `document.create` / `document.update` в ветке global-server-side (рядом с наполнением `media_accumulator`, `:3204-3211`); реестр `_DOCUMENT_MUTATING_TOOL_NAMES = {document.create, document.update}` (`_DOCUMENT_TOOL_NAMES` на `:135` включает чтение и для этой цели **не** годится).
3. **Turn-scope**: `_resolve_turn_documents` / `_with_turn_documents` по образцу `:2284-2314`, восстановление через существующий `tool_results_for_message_step`; вызов из `_decorate_turn_out` (`:2354`) — этого достаточно, чтобы поле легло на все пять терминальных ног хода.
4. **Свёртка**: last-wins по `documentId`, порядок — по первому появлению; пустой результат → `None`.
5. **Ответ** (`src/app/api_gateway/routers/chat.py`): маппинг в `_to_response` (`:454`) рядом с `mediaJobs` — legacy, v2 и SSE-`done` получают поле автоматически, отдельных правок маршрутов не требуется.
6. **Swagger**: описание поля — без внутренних ссылок на ADR/TD/Q (правило пользовательских описаний OpenAPI).

> Фаза 2 самодостаточна: миграции нет, контракт REST не меняется, биллинг не меняется, обратная совместимость полная (поле nullable, старые клиенты игнорируют).

## Не запланировано (требует отдельного решения)
- `If-Match` / `409 conflict` для конкурентных правок — сегодня `version` в конкурентности не участвует ([02-api-contracts §version](02-api-contracts.md#version--признак-изменения-а-не-механизм-конкурентности)).
- Якорь `payload.documents` в истории `GET /v1/chats/{id}` — отвергнут [ADR-101 §9](../../adr/ADR-101-chat-response-documents.md).
- Устранение асимметрии «пустое содержимое» между `document.create` и остальными тремя путями ([02-api-contracts §Обязательность непустого содержимого](02-api-contracts.md#обязательность-непустого-содержимого-у-documentcreate)).
- Рендер в PDF/docx ([Q-090-1](../../99-open-questions.md)).
