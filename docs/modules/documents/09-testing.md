# Documents — Testing

Реализация — [ADR-090](../../adr/ADR-090-chat-documents.md) / [ADR-101](../../adr/ADR-101-chat-response-documents.md). Стратегия — [06-testing-strategy.md](../../06-testing-strategy.md). Ниже — **перечень требований к покрытию** (что обязано быть проверено); сами тесты пишет и запускает qa.

## Unit — нормализация имени (`_normalize_filename`)
Каждая строка — отдельный кейс; общий тест «имя нормализуется» перечень не закрывает.
- пустое / `null` / из одних пробелов → `document` + расширение типа;
- `C:\Users\x\отчёт.md` и `../../etc/passwd` → последний сегмент, разделители вырезаны;
- имя уже с ожидаемым расширением не трогается, регистр сохраняется (`ОТЧЁТ.MD` при `text/markdown`);
- **снятие чужого ИЗВЕСТНОГО расширения**: `список.txt` при `text/markdown` → `список.md` (не `список.txt.md`); симметрично для `.csv`, `.json`, `.md` во всех четырёх типах;
- **неизвестное расширение остаётся частью имени**: `договор.docx` при `text/markdown` → `договор.docx.md`; `v1.2` → `v1.2.md`;
- имя из одного расширения (`.md` при `text/csv`) не теряет основу → `.md.csv`;
- усечение до 200 **после** добавления расширения: имя в 200 символов теряет хвост расширения.

## Unit — лимиты и потолки
- документ больше `DOCUMENT_MAX_BYTES` → `document_too_large`;
- создание при уже достигнутом `DOCUMENT_MAX_COUNT` → `too_many_documents`; **обновление при том же условии проходит** (число документов при обновлении не проверяется);
- превышение `DOCUMENT_TOTAL_BYTES` → `documents_total_too_large`; при обновлении заменяемый размер **выбывает** из суммы (правка на один байт документа, занимающего весь потолок, проходит);
- `mediaType` вне четырёх → `unsupported_media_type`;
- `size` считается в **байтах UTF-8**: кириллическая строка даёт больше байт, чем символов;
- строка контекста режется до `DOCUMENT_CONTEXT_MAX_CHARS`; при отсутствии документов — `None`.

## Integration — REST
- `POST` своей сессии → `201`, `version=1`, `createdBy="user"`, `content=null` в ответе;
- `POST` в чужую/несуществующую сессию → `404 session_not_found`; `GET` списка там же → `404 session_not_found`;
- `GET` списка своей пустой сессии → `200` с пустым массивом (не `404`);
- `GET {documentId}` → `content` в base64; в списке `content=null`;
- `PATCH` → содержимое заменено **целиком**, `version` +1, `filename`/`mediaType`/`createdBy`/`createdAt` не изменились;
- `DELETE` → `{deleted:true}`; повторный → `404 document_not_found`;
- чужой документ на `GET`/`PATCH`/`DELETE`/`download` → `404 document_not_found` (не `403`, не `200`);
- невалидный base64 → `422 validation_error`; содержимое не UTF-8 → `422 validation_error`; лишний ключ в теле → `422`;
- `download`: тело в UTF-8, `Content-Type` = `mediaType`, `Cache-Control: private, no-store`, `Content-Disposition` содержит **оба** параметра, кириллическое имя приезжает в `filename*`, при полностью не-ASCII имени `filename` = `document` + расширение;
- `download` **без** заголовка `Authorization` → `401` (проверяет, что подписанного URL нет и ссылку нельзя отдать предпросмотру).

## Integration — конкурентность и `version`
- `If-Match` в запросе `PATCH` **игнорируется** (заголовок не читается); `ETag` не отдаётся;
- поле `version` в теле `PATCH` → `422` (строгая схема);
- два последовательных `PATCH` → `version` +2, содержимое от последнего; **условной проверки нет** — тест фиксирует именно это поведение, чтобы введение `If-Match` не прошло молча.

## Integration — инструменты модели
- `document.create` без `content` и с пустым `content` → tool-result ошибка `invalid_document_request`, **ход не падает**;
- `document.update` без `content` → документ **опустошается**, `version` +1 (обратная сторона предыдущего кейса — обе стороны асимметрии покрыты);
- `document.create` без `mediaType` → дефолт `text/markdown`, ход не падает (регрессия прода 2026-08-24);
- ключ в неверном регистре (`mediatype`) → tool-result ошибка, а не `422` на весь ход (та же регрессия);
- `documentId` не uuid → tool-result ошибка `invalid_document_request`;
- документ чужой сессии по id → tool-result ошибка, а не доступ;
- `document.create` кладёт `createdBy="assistant"`; последующий пользовательский `PATCH` его **не меняет**;
- `document.list` / `document.read` возвращают карточку с `size` и `version`; `read` дополнительно `content`;
- `GlobalToolHandlers` без `DocumentsService` → `documents_not_available`, ход не падает;
- строка о документах появляется в системном промте, когда документ есть, и отсутствует, когда его нет.

## Integration — каскад и модерация
- `DELETE /v1/chats/{id}` → документы чата исчезли из БД (проверка на уровне таблицы, а не только `404` в API);
- удаление пользователя → документы исчезли;
- ход, создающий документ с содержимым, которое модерация отклонила бы, **проходит**: провайдер модерации на этом пути не вызывается (проверяется отсутствием обращения к нему, а не «нет ошибки»).

## Integration — `documents[]` в ответе хода ([ADR-101](../../adr/ADR-101-chat-response-documents.md))
Каждый пункт — отдельный кейс; тест «поле присутствует» перечень не закрывает.
- ход с одним `document.create` → `documents[0]` = `{documentId, filename, mediaType, size, version=1}`, `content` в элементе **отсутствует**;
- `documentId` из `documents[]` резолвится в `GET /v1/chats/{sessionId}/documents/{documentId}` → `200` (сквозная проверка цепочки, а не только формы элемента);
- `filename` в `documents[]` равен **нормализованному** имени, а не отправленному (кейс `список.txt` при `text/markdown`);
- ход только с `document.read` → `documents = null`; ход только с `document.list` → `documents = null`;
- ход, где `document.create` завершился ошибкой (лимит / пустое содержимое) → `documents = null`, а факт отказа виден в `serverTools[]` со `status="errored"`;
- ход без документов вообще → `documents = null` (**не** `[]`);
- policy-`blocked` → `documents = null`;
- **turn-scope**: `create` на первом витке + client-side инструмент → поле присутствует и в ответе `/chat/run`, и в ответе `/chat/tool-result`, закрывающего ход;
- **идемпотентный повтор** `/chat/tool-result` закрытого хода → поле **восстановлено** (в отличие от `serverTools[]`, который на том же повторе пустой — обе стороны контраста в одном тесте);
- `blocked` + `blockReason=max_tokens` после успешного `create` → поле присутствует;
- **дедупликация**: `create` + `update` одного документа в одном ходе → **одна** запись с `version=2`; два `update` подряд → одна запись с последним `version`; порядок — по первому появлению;
- два разных документа в одном ходе → две записи в порядке первого появления;
- поле присутствует в ответах `/v1/chat/run`, `/v1/chat/tool-result`, `/v1/chat/v2/run`, `/v1/chat/v2/tool-result` и в SSE-событии `done`;
- **обратная совместимость**: ответ без документов сериализуется с `documents: null` и не ломает существующие проверки схемы.

## Изоляция
Все маршруты и все инструменты скоупятся `sub` + `sessionId`; ни один кейс не должен проходить с чужим владельцем.
