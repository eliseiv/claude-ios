# Module: Documents (документы чата)

- Статус: **Реализован ([ADR-090](../../adr/ADR-090-chat-documents.md))** — код в `src/app/documents/` + роутер `src/app/api_gateway/routers/documents.py` + global tools `document.*` + миграция `0027`. ⏳ доработка [ADR-101](../../adr/ADR-101-chat-response-documents.md) (`ChatResponse.documents[]`) — **код написан, ПОКРЫТ автотестами, слит в `main` и ВЫКАЧЕН на инстансы; ревью НЕ проходило**. Обе позиции правки 2026-09-08 (тип `mediaType`, безусловное восстановление по ходу) в коде закрыты; незакрытый остаток — покрытие тестами.
- Ответственность: персистентный **текстовый** документ, привязанный к чату; создаётся и пользователем (`POST /v1/chats/{sessionId}/documents`), и моделью (`document.create`), дальше читается, правится и скачивается одинаково. **НЕ путать** с `files.*` (исполняет устройство пользователя), `site.*` (website-builder) и файлами-знаниями workspace ([ADR-036](../../adr/ADR-036-workspaces-implementation.md)) — разведение в [01-context.md](01-context.md).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> **Data model ([ADR-090 §9](../../adr/ADR-090-chat-documents.md)):** таблица `chat_documents` ([03-data-model §22](../../03-data-model.md)) — содержимое в колонке `TEXT` (не `BYTEA`: формат текстовый по решению), `ON DELETE CASCADE` по `session_id` и по `user_id`. Создана миграцией **`0027`** (expand-only; цепочка ревизий и head в `docs/` не перечисляются — [07-deployment.md §Миграции](../../07-deployment.md#миграции)).

> **Почему этот модуль появился поздно.** Возможность выкачена [ADR-090](../../adr/ADR-090-chat-documents.md) 2026-08-24, а модульного ТЗ не имела: контракт жил только внутри ADR, в `docs/API-REFERENCE.md` слово «documents» не встречалось ни разу. Измерение 2026-09-08 на `velunixa`: 1230 документов, **все** созданы моделью, пользователями — ни одного. Серверная часть работает под нагрузкой, приложение ею не пользуется. Модуль и раздел [API-REFERENCE §30](../../API-REFERENCE.md#30-documents-документы-чата) закрывают именно этот разрыв.

## DoD
- **Реализовано ([ADR-090](../../adr/ADR-090-chat-documents.md)):** REST `POST/GET/PATCH/DELETE /v1/chats/{sessionId}/documents[/{documentId}]` + `GET .../{documentId}/download`; global server-side tools `document.create` / `document.list` / `document.read` / `document.update`; строка о документах в системном промте; лимиты `DOCUMENT_MAX_BYTES` / `DOCUMENT_MAX_COUNT` / `DOCUMENT_TOTAL_BYTES` / `DOCUMENT_CONTEXT_MAX_CHARS`; изоляция владельца `404` на каждом пути; миграция `0027`.
- **Написано, ПОКРЫТО автотестами, слито в `main` и ВЫКАЧЕНО; ожидает ревью ([ADR-101](../../adr/ADR-101-chat-response-documents.md)):** поле `ChatResponse.documents[]` — ход, в котором модель звала `document.create`/`document.update`, возвращает приложению карточки затронутых документов (`documentId`, `filename`, `mediaType`, `size`, `version`); turn-scoped (восстановление по ходу безусловное), дедуплицировано по `documentId`, `null` при отсутствии правок. Доменный слой документов при этом **не меняется**. Незакрытый остаток — **ревью**: §1 (`mediaType` — перечисление) и §4 (безусловное восстановление и слияние источников) в коде закрыты и покрыты — `tests/integration/test_chat_response_documents_adr101.py`, **23** кейса (измерено 2026-09-10: `grep -c '^\s*\(async \)\?def test_'`). Прежняя формулировка «ни одного кейса на `ChatResponse.documents[]` в `tests/` нет» противоречила началу этой же строки и на 2026-09-10 ложна.

## Changelog
- 2026-09-08 ([ADR-101](../../adr/ADR-101-chat-response-documents.md), docs-only — правка ТЗ по находке исполнителя): **снято внутреннее противоречие §3 ↔ §4** — восстановление по ходу стало **безусловным** (прежнее «когда аккумулятор пуст» теряло документ раннего витка, если в текущем вызове тронут ещё один, и давало разный ответ на разных ногах одного хода), источники сливаются, свёртка §5 применяется к объединённому списку; **`mediaType` в элементе — перечисление из четырёх значений**, а не свободная строка. Код, написанный по прежней редакции, требует доработки. Scope backend + qa.
- 2026-09-08 ([ADR-101](../../adr/ADR-101-chat-response-documents.md), docs-only — ТЗ для backend): **bootstrap модуля + поле `ChatResponse.documents[]`**. Зафиксирован контракт, до сих пор доступный только чтением кода: `PATCH` заменяет содержимое целиком, правила нормализации имени (`_normalize_filename`), `version` вне конкурентности (`If-Match` нет), `createdBy` — происхождение, а не право, скачивание под JWT, ограничение частоты на скачивании, каскад по чату, отсутствие модерации, обязательность непустого `content` у `document.create`. Новое — только поле ответа. Scope backend + qa.
