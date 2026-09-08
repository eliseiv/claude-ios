# Documents — Implementation Phases

## Фаза 1 — ядро ([ADR-090](../../adr/ADR-090-chat-documents.md)) — **РЕАЛИЗОВАНА** (2026-08-24)

1. Миграция **`0027`**: таблица `chat_documents` + индекс `ix_chat_documents_session_created` (expand-only; цепочка ревизий и head в `docs/` не перечисляются — [07-deployment.md §Миграции](../../07-deployment.md#миграции)).
2. `DocumentsService` (`src/app/documents/`): CRUD, потолки на сессию, нормализация имени, строка контекста для промта.
3. Роутер `/v1/chats/{sessionId}/documents` + `/download`, строгие схемы, `Content-Disposition` по RFC 5987.
4. Инструменты `document.create` / `document.list` / `document.read` / `document.update` в `GLOBAL_SERVER_SIDE_TOOLS`; регистрация в `MUTATING_TOOLS` и `ARGS_DEGRADE_TOOLS`; domain↔anthropic маппинг имён.
5. Инъекция строки о документах в системный промт.

## Фаза 2 — `ChatResponse.documents[]` ([ADR-101](../../adr/ADR-101-chat-response-documents.md)) — **код написан по действующей редакции ADR, автотестами не покрыт, ревью не проходило, в `main` не слит и не выкачен**

> Пункты **1** (тип `mediaType`) и **3** (безусловное восстановление и слияние источников) писались по прежней редакции ADR и с тех пор **приведены к действующей** — оба закрыты в коде (`ChatDocumentRefSchema.mediaType: DocumentMediaType`; `ChatOrchestrator._resolve_turn_documents` восстанавливает безусловно и сливает источники). Незакрытый остаток фазы — **покрытие автотестами**: ни одного кейса на `ChatResponse.documents[]` в `tests/` нет, поэтому гарантия [ADR-101 §3](../../adr/ADR-101-chat-response-documents.md) «все ноги хода отвечают одним составом» ничем не застрахована от регресса.

Только проекция уже существующего состояния в ответ хода. Доменный слой документов, схемы REST, лимиты, миграции — **не трогаются**.

1. **Схема** (`src/app/schemas/chat.py`): `ChatDocumentRefSchema(StrictModel)` — `documentId: uuid`, `filename: str`, `mediaType: Literal["text/markdown", "text/plain", "text/csv", "application/json"]` (перечисление, а не `str`: домен закрыт на записи, и та же величина уже публикуется перечислением в REST-объекте документа — [ADR-101 §1](../../adr/ADR-101-chat-response-documents.md)), `size: int`, `version: int` (образец — `MediaJobRefSchema`, `:530`). Поле `documents: list[ChatDocumentRefSchema] | None = None` в `ChatResponse` рядом с `mediaJobs` (`:764`).
2. **Транспорт хода** (`src/app/chat/orchestrator.py`): поле `documents: list[dict[str, Any]] | None = None` в `ChatRunOut` (рядом с `media_jobs`, `:843`); аккумулятор по образцу `_MediaJobsAccumulator`, наполняемый успешными результатами `document.create` / `document.update` в ветке global-server-side (рядом с наполнением `media_accumulator`, `:3204-3211`); реестр `_DOCUMENT_MUTATING_TOOL_NAMES = {document.create, document.update}` (`_DOCUMENT_TOOL_NAMES` на `:135` включает чтение и для этой цели **не** годится).
3. **Turn-scope**: `_resolve_turn_documents` / `_with_turn_documents` по образцу `:2284-2314`, восстановление через существующий `tool_results_for_message_step`; вызов из `_decorate_turn_out` (`:2354`) — этого достаточно, чтобы поле легло на все пять терминальных ног хода. **Отличие от образца:** восстановление вызывается **всегда** при непустом `message_step_id`, а не только при пустом аккумуляторе, и его результат **сливается** с аккумулятором (сначала восстановленные записи в порядке `seq ASC`, затем записи аккумулятора) — [ADR-101 §4](../../adr/ADR-101-chat-response-documents.md). Копировать условие `if not accumulator` у `mediaJobs` **нельзя**: именно оно теряет документ раннего витка, когда в текущем вызове тронут ещё один. То же условие признано дефектом и у самого образца — [ADR-103](../../adr/ADR-103-media-jobs-turn-scoped-merge.md) предписывает снять его и там (отдельная задача `backend`), но пока она не выполнена, образец в коде остаётся прежним.
4. **Свёртка**: last-wins по `documentId`, порядок — по первому появлению; применяется к **объединённому** списку обоих источников (пересечение ожидаемо и схлопывается); пустой результат → `None`.
5. **Ответ** (`src/app/api_gateway/routers/chat.py`): маппинг в `_to_response` (`:454`) рядом с `mediaJobs` — legacy, v2 и SSE-`done` получают поле автоматически, отдельных правок маршрутов не требуется.
6. **Swagger**: описание поля — без внутренних ссылок на ADR/TD/Q (правило пользовательских описаний OpenAPI).

> Фаза 2 самодостаточна: миграции нет, контракт REST не меняется, биллинг не меняется, обратная совместимость полная (поле nullable, старые клиенты игнорируют).

## Не запланировано (требует отдельного решения)
- `If-Match` / `409 conflict` для конкурентных правок — сегодня `version` в конкурентности не участвует ([02-api-contracts §version](02-api-contracts.md#version--признак-изменения-а-не-механизм-конкурентности)).
- Якорь `payload.documents` в истории `GET /v1/chats/{id}` — отвергнут [ADR-101 §9](../../adr/ADR-101-chat-response-documents.md).
- Устранение асимметрии «пустое содержимое» между `document.create` и остальными тремя путями ([02-api-contracts §Обязательность непустого содержимого](02-api-contracts.md#обязательность-непустого-содержимого-у-documentcreate)).
- Рендер в PDF/docx ([Q-090-1](../../99-open-questions.md)).
