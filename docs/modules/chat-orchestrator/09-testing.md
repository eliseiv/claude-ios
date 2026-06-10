# Chat Orchestrator — Testing

## Unit
- Tool-схемы: валидные/невалидные args/result для всех 8 tools → 422 на нарушение.
- `path` traversal (`..`) отклоняется.
- Маппинг ответа Anthropic (end_turn/tool_use) → status.
- usage parsing включая cache_read/cache_creation.
- **tool_use.id (BUG-4, ADR-008):** разбор `tool_use` с реалистичным anthropic id (`toolu_01...`, **не** UUID) → `tool_calls.provider_tool_use_id` = raw id; `tool_calls.id` = свежий UUID (не выведен из anthropic id); наружу `toolCall.id` = доменный UUID.
- **Нормализация payload (BUG-5, ADR-021):** assistant `tool_use`-блок из ответа SDK со служебным полем `caller` (`block.model_dump()`) → в `chat_steps.payload` сохранены только wire-валидные поля (`type`/`id`/`name`/`input`), `caller` отсутствует; raw `tool_use.id` сохранён дословно. Реконструированные `messages` к Anthropic не содержат `caller`.

> **Требование к fake/мокам Anthropic-клиента:** во ВСЕХ тестах (unit/integration/e2e) fake `messages.create` обязан возвращать `tool_use.id` в **реалистичном** формате `toolu_<...>` (НЕ UUID-образный). Старый fake отдавал UUID-образный id и маскировал BUG-4. Запрет UUID-образного provider id в fake — нормативное требование тестовой инфраструктуры.

## Integration (respx для Anthropic)
- `/chat/run` blocked: для каждого blockReason возвращается 200 + reason, генерация не вызвана.
- `/chat/run` allow → assistant_message; chat_steps записан; audit chat_step.
- tool_use → status=tool_call, tool_calls(pending) создан, audit tool_call_initiated.
- `/chat/tool-result` чужой/несуществующий toolCallId → 404/403.
- Повторный tool-result с completed → идемпотентно, Anthropic не вызван повторно.
- mode=byok → используется ключ пользователя (проверка через мок BYOK), ключ не в логах/steps.

## Integration — порядок шагов server-side tool-loop (BUG-5, ADR-021)
- **Детерминированный порядок при равном `created_at`:** server-side tool (`site.*`) пишет `tool_use`-шаг и `tool_result`-шаг в **одной транзакции** (равный `created_at`). Реконструкция (`_build_messages` через `list_steps`) должна давать `messages` в порядке `assistant(tool_use) → user(tool_result)` **независимо** от значений `id`/`created_at`. Тест должен ставить такой `id`, при котором старая `(created_at, id)`-сортировка инвертировала бы порядок (UUID `tool_result` < UUID `tool_use`) → на старой реализации orphan tool_result/400, на новой (`ORDER BY seq`) — корректно.
- `next_step_after` возвращает следующий шаг по `seq`, не по `created_at`.

## Integration — sync ids в `ChatResponse` (ADR-023)

Нормативное покрытие инварианта синка `messageStepId` / `stepId` ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)).

- **Непустые id при `assistant_message` / `tool_call`:** ответы `/v1/chat/run` и `/v1/chat/tool-result` со `status=assistant_message` либо `status=tool_call` несут **НЕПУСТЫЕ** `messageStepId` и `stepId` (оба не `null`).
- **`stepId` точно совпадает с историей:** `ChatResponse.stepId` **дословно равен** `ChatStepSchema.id` соответствующего шага в `steps[]` ответа `GET /v1/chats/{id}` (точное совпадение UUID — шаг-носитель: финальный assistant-шаг при `assistant_message`, assistant-шаг с `tool_use`-блоком при `tool_call`).
- **`messageStepId` стабилен в пределах хода:** `messageStepId`, выданный в `/v1/chat/run`, **равен** `messageStepId` в ответе последующего `/v1/chat/tool-result` того же хода (run → tool-result одного хода дают равный `messageStepId`).
- **`blocked` → оба `null`:** при `status=blocked` `messageStepId` = `null` и `stepId` = `null` (шаг/ход не создаются — блок до генерации, [ADR-004](../../adr/ADR-004-blocked-http-200.md)).
- **`stepId`/`messageStepId` ≠ `toolCall.id`:** при `status=tool_call` ни `stepId`, ни `messageStepId` **не равны** `toolCall.id` — это разные идентификаторы (id шага/хода vs доменный `tool_calls.id`, [ADR-008](../../adr/ADR-008-provider-tool-use-id.md)).

## Integration — История: доменная нормализация payload (ADR-024)

Нормативное покрытие нормализации `GET /v1/chats/{id}` → `steps[].payload` ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)). Fake Anthropic возвращает `tool_use.id = "toolu_..."` и `tool_use.name` в underscore-формате (инвариант fake, см. выше).

- **Имя — dot, == `/v1/tools`:** `steps[].payload.content[]` с `type=tool_use` отдаёт `name` в доменном dot-формате (`calendar.create_events`), **дословно равном** `name` соответствующего инструмента в `GET /v1/tools` и `toolName` в `GET /v1/chats/{id}/steps`.
- **id — domain, == `/chat/run` `toolCall.id`:** `tool_use.id` в истории **дословно равен** `toolCall.id`, который `/chat/run` вернул для этого вызова (= `tool_calls.id`), а **не** provider `toolu_...`.
- **`tool_result.tool_use_id` == тот же domain id:** блок `tool_result` в истории несёт `tool_use_id`, равный domain `tool_calls.id` породившего `tool_use` (та же доменная пара).
- **Provider id не утекает:** ни в одном блоке ответа `GET /v1/chats/{id}` нет строки `toolu_...`.
- **Текстовые блоки целы:** `type=text`-блоки и `tool_use.input` отдаются байт-в-байт как в хранилище (не модифицированы).
- **Полнота шага `[text, tool_use]`:** assistant-шаг, чей `payload.content` содержит и `text`, и `tool_use` (один ход Claude), отдаётся **полностью** — оба блока присутствуют в `steps[].payload.content[]` в исходном порядке. (Опционально: parallel tool use — несколько `tool_use`, каждый со своим domain id.)
- **Хранение не мутировано:** после отдачи истории `chat_steps.payload` в БД по-прежнему содержит underscore-имя и provider `toolu_...` (нормализация — на копии при сериализации, не in-place); реплей `_build_messages` не сломан.
- **Без N+1:** карта `provider_tool_use_id → domain id` строится одним запросом на сессию (проверка числа запросов на отдачу истории с многораундовым tool-loop).

### `assistantMessage` при `tool_call` (ADR-024 п.3 / Q-024-1, вариант A)

Нормативное покрытие enrichment `ChatResponse` сопутствующим текстом ([Q-024-1](../../99-open-questions.md) Closed = вариант A, [ADR-024 §Decision п.3](../../adr/ADR-024-history-payload-domain-normalization.md)).

- **Текст + tool_use → assistantMessage непустой:** когда assistant-ход Claude несёт `[text, tool_use]` (fake Anthropic возвращает оба блока в одном сообщении), ответ `/chat/run` (и `/chat/tool-result`) имеет `status=tool_call`, **непустой** `toolCall` (обязателен) И **непустой** `assistantMessage`, равный тексту `text`-блока(ов) того же шага.
- **tool_use без текста → assistantMessage null:** assistant-ход с одним `tool_use` без `text`-блока → `status=tool_call`, `toolCall` непустой, `assistantMessage = null`/опущен.
- **Совпадение с историей:** `assistantMessage` при `tool_call` **дословно равен** конкатенации `text`-блоков шага `stepId` в `GET /v1/chats/{id}` → `steps[].payload.content[]` (тот же шаг, на который указывает `ChatResponse.stepId`; нормализация текстовые блоки не меняет).
- **Обратная совместимость финала/blocked:** при `status=assistant_message` `assistantMessage` = финальный текст (без изменений); при `status=blocked` `assistantMessage = null`.

## E2E (AC-4)
- Полный tool-loop: run → tool_call → tool-result → tool_call → ... → assistant_message (≥2 итерации).
- **Server-side tool-loop continuation (BUG-5 регресс, live):** website-builder `site.*` multi-round tool-loop с реальным Claude → реконструкция диалога корректна (нет orphan tool_result, нет Anthropic 400/502). Покрывается live e2e website-builder после восстановления org Anthropic (см. memory/deployment-state).
- **Continuation с реалистичным anthropic id (BUG-4 регресс):** fake возвращает `tool_use.id = "toolu_..."`; на раунде continuation проверить, что отправленный в Anthropic `tool_result.tool_use_id` **точно равен** этому raw id (а не доменному UUID), и реплеенный assistant `tool_use.id` совпадает с ним → второй `messages.create` не падает с 400. Тест должен падать на старой реализации (`uuid4`-подмена).
