# Chat Orchestrator — Testing

## Unit
- Tool-схемы: валидные/невалидные args/result для **всех** инструментов реестра `_ARGS_BY_TOOL` (число не дублируется здесь — состав и счётчик в [02-api-contracts.md §GET /v1/tools](02-api-contracts.md#get-v1tools--каталог-инструментов-adr-019)) → `422` на нарушение. **Исключение — инструменты из `ARGS_DEGRADE_TOOLS`** (`quiz.generate`): у них нарушение даёт tool-result `invalid_quiz`, а не `422` ([§Study & Learn](#integration--study--learn-квиз-adr-064)).
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
- **Обратная совместимость финала/blocked:** при `status=assistant_message` `assistantMessage` = финальный текст (без изменений); при `status=blocked` `assistantMessage = null`. **Область действия этих кейсов — ходы БЕЗ квиза:** при непустом `quiz` `assistantMessage` подавляется при любом статусе ([ADR-064 §7](../../adr/ADR-064-study-learn-quiz-generation-mode.md), кейсы — [§Study & Learn](#integration--study--learn-квиз-adr-064)). Фикстуры этой секции обязаны быть не-квиз ходами, иначе ассерт «текст непуст» разойдётся с ADR-064.

## Integration — Параллельные tool-вызовы + max_tokens (ADR-025)

Нормативное покрытие [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md). Fake Anthropic возвращает `tool_use.id = "toolu_..."` (инвариант fake).

### Параллельные client-side tool-вызовы (`toolCalls[]`, барьер хода)
- **Все client-side вызовы surface'ятся:** assistant-ход с ≥2 client-side `tool_use`-блоками (например два `files.write`) → `/chat/run` `status=tool_call`, `toolCalls[]` содержит **все** вызовы (в порядке блоков), каждый со своим domain `id`/`name`/`args`; `toolCall` (одиночный) = `toolCalls[0]`. **Тест должен падать на старой реализации** (`first_client_out` — только первый).
- **stepId один на ход:** все элементы `toolCalls[]` принадлежат одному `stepId` (assistant-шаг с несколькими `tool_use`-блоками); `messageStepId` — один ход.
- **Барьер хода — continuation только при всех результатах:** прислать `/chat/tool-result` с результатом **одного** из двух tool-вызовов → ответ снова `status=tool_call` с **оставшимся** `toolCalls[]`, Anthropic **не** вызван, кредит не списан. Прислать результат второго → барьер закрыт → continuation-виток (следующий шаг). Батч-форма (`results=[r1,r2]` в одном запросе) → барьер закрыт сразу.
- **Server-side в toolCalls[] не попадает:** смешанный ход (`site.write_file` + `files.write`) → `site.*` исполнен на бэке, в `toolCalls[]` только client-side `files.write`; continuation собирает `tool_result` обоих (server-side + client-side) перед `messages.create`.
- **Идемпотентность:** повторный `toolCallId` (completed) в батче/запросе → результат не перезаписан, continuation не дублируется; дубль `toolCallId` в одном батче → `422`.
- **Обратная совместимость:** одиночная форма запроса (`toolCallId`+`result|error`) эквивалентна батчу из одного; одиночный `toolCall` в ответе = `toolCalls[0]`.
- **Биллинг неизменен:** ход с несколькими параллельными tool-вызовами и батч-результатами списывает **ровно 1** кредит на финальном `assistant_message` (идемпотентно по `messageStepId`).
- **Инвариант синка истории:** `toolCalls[i].name`/`.id` == соответствующий `tool_use`-блок шага `stepId` в `GET /v1/chats/{id}` == `/v1/tools` `name` ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)).

### Обрезка по max_tokens (`blockReason=max_tokens`)
- **stop_reason=max_tokens → blocked(max_tokens):** fake Anthropic возвращает `stop_reason="max_tokens"` c content, содержащим `text` + неполный `tool_use` → `/chat/run` `status=blocked`, `blockReason=max_tokens`. **`toolCall`/`toolCalls` отсутствуют** (неполные tool_use не отдаются). **Тест должен падать на старой реализации** (уходило в `assistant_message`, `toolCall=null`).
- **id/usage присутствуют (отличие от policy-blocked):** при `blockReason=max_tokens` `messageStepId`/`stepId` — **НЕ** null (ход/обрезанный assistant-шаг созданы), `usage` присутствует; `assistantMessage` = частичный текст (если был).
- **Кредит не списан:** `mode=credits` ход, оборванный по `max_tokens`, не списывает кредит и не флипает trial (баланс/`trial_used` не меняются).
- **policy-blocked не регрессировал:** policy-deny (например `credits_empty`) по-прежнему `messageStepId=null`/`stepId=null`/без `usage`.
- **Дефолт max_tokens:** `ANTHROPIC_MAX_TOKENS` дефолт = `16000` (проверка config-дефолта); `ANTHROPIC_TIMEOUT_SECONDS` дефолт = `120`.

## Unit — каталог инструментов и утечка внутренних идентификаторов

Нормативное покрытие формата `inputSchema` и инварианта «внутренние идентификаторы не покидают процесс» ([02-api-contracts.md §inputSchema](02-api-contracts.md#inputschema--нормативный-формат)).

- **Детектор утечки (diff, обязателен):** прогнать **все** записи `tool_catalog()` (поля `description` и `inputSchema`) **и** все определения `neutral_tool_definitions(...)` для **каждого** значения `generationMode` через регулярку `ADR-\d+|TD-\d+|Q-\d+-\d+|BUG-\d+|MAX_SERVER_TOOL_ROUNDS|GlobalToolHandlers|SiteToolHandlers|_ARGS_BY_TOOL|[A-Za-z]+Args` → **ноль** совпадений. Тест обязан **падать**, если вырезание модельной метаинформации снято: докстринги моделей args содержат ADR-ссылки и имена внутренних классов намеренно (это внутренняя документация), поэтому детектор ловит регресс сразу. Проверка ведётся по **всему** каталогу, а не по инструментам, которые правились: утечка приходит от любой модели с содержательным docstring.
- **Вырезано ровно нужное:** у каждой записи каталога в корне `inputSchema` **отсутствуют** `title`/`description`; при этом у полей (`properties.*`) описания, заданные через `Field(description=...)`, **присутствуют и непусты** там, где они заданы, а ограничивающие ключи (`minItems`/`maxItems`/`maxLength`) сохранены. Тест ловит противоположный регресс — «вырезали слишком много».
- **Self-contained схемы:** для инструментов из соответствующего реестра (`quiz.generate`) схема не содержит ни `$defs`, ни `$ref`, и внутри инлайненных определений тоже нет корневых `title`/`description` вложенной модели.
- **Каталог как целое — сверка СОСТАВА с независимым объявлением (несущая проверка):** множество `{e["name"] for e in tool_catalog()}` **равно** `ALL_TOOL_NAMES`. `ALL_TOOL_NAMES` объявлен **отдельным** перечислением имён, а не выведен из `_ARGS_BY_TOOL`, поэтому расхождение реестров (инструмент добавлен в один и забыт в другом) роняет тест. Дополнительно: порядок детерминирован (совпадает с порядком объявления `_ARGS_BY_TOOL`), у каждой записи непустой `description` и `inputSchema` с `type: object`.
  > **Чего эта секция НЕ проверяет (и почему это записано):** сравнение `len(tool_catalog()) == len(_ARGS_BY_TOOL)` — сверка генератора с тем самым словарём, который он обходит: обе стороны движутся вместе, поэтому оно ловит только пропуск/дубль **внутри цикла** и **не способно** поймать дрейф реестров. Держать его допустимо как дешёвую проверку самого цикла, но **засчитывать за сверку каталога — нельзя**: несущая проверка — равенство состава `ALL_TOOL_NAMES` выше.
- **Влияние [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md) выражается через СОСТАВ и СХЕМУ, а не через счётчик:** отдельного теста «число инструментов не изменилось из-за ADR-027» быть не должно — в терминах реестра это утверждение невыразимо и дублирует каталожный тест. Проверяется то, что ADR-027 действительно менял: (а) `{имена каталога}` **равно** `ALL_TOOL_NAMES` (состав не тронут — покрыто пунктом выше); (б) `inputSchema` записи `calendar.read` содержит свойства `start`/`end` и **не** содержит `startDate`/`endDate`; (в) описание `calendar.read` в каталоге называет формат ISO8601-datetime и end-exclusive-конвенцию (требование [§Контракт календарных инструментов](02-api-contracts.md#контракт-календарных-инструментов-startend-нормативно-adr-027)).

## Integration — Study & Learn (квиз, ADR-064)

Нормативное покрытие режима `study_learn` и инструмента `quiz.generate` ([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)). Fake-клиент провайдера возвращает `tool_use` с реалистичным provider-id (общий инвариант fake выше) и underscore-именем `quiz_generate`.

> **Правило приёмки этой секции:** тест, который сам конструирует пул и проверяет только валидатор, **не засчитывается** как покрытие фичи — он проверяет компонент, а не цепь. Для каждой цепочки `producer → consumer` из [ADR-064 §12](../../adr/ADR-064-study-learn-quiz-generation-mode.md) нужен прогон **рабочего пути** (HTTP-вызов эндпоинта) и проверка, что артефакт дошёл до потребителя. Помеченные **diff** пункты обязаны **падать** на реализации без соответствующего фикса — иначе покрытие фиктивно.

### Unit — реестры и схема инструмента
- **Каталог:** `tool_catalog()` содержит запись `quiz.generate` (`execution="server"`, `mutating=false`), состав каталога **сверяется с независимо объявленным `ALL_TOOL_NAMES`** (см. [§Unit — каталог инструментов](#unit--каталог-инструментов-и-утечка-внутренних-идентификаторов): сверка `len(...)` с `_ARGS_BY_TOOL` тавтологична и несущей не считается), а не с литералом-числом; порядок детерминирован. Нормативный состав/счётчик — [02-api-contracts §GET /v1/tools](02-api-contracts.md#get-v1tools--каталог-инструментов-adr-019).
- **Схема self-contained (diff):** `tool_input_schema("quiz.generate")` **не содержит** `$ref`/`$defs` (вложенная модель вопроса инлайнена); ограничивающие ключи (`minItems`/`maxItems`/`maxLength`) присутствуют. Тест падает на «наивной» реализации через `model_json_schema()` без инлайна.
- **Ось C — гейт (diff):** `neutral_tool_definitions(generation_mode="study_learn")` содержит `quiz.generate`; при `general`/`research`/`reasoning` и при вызове **без** параметра — **не** содержит. Тест падает, если инструмент добавлен в набор безусловно.
- **Ось C — sweep по соседям:** для **всех остальных** инструментов реестра (весь `_ARGS_BY_TOOL` минус `quiz.generate` — число не дублируется в тесте, берётся из реестра) состав набора одинаков во всех четырёх режимах при равных осях A/B (ось C не задела ни одного соседа). Проверяется как равенство множеств имён `set(mode) - {"quiz.generate"}` для каждой пары режимов.
- **Реестры непересекающиеся:** `SERVER_SIDE_TOOLS ∩ GLOBAL_SERVER_SIDE_TOOLS = ∅`; `quiz.generate ∈ GLOBAL_SERVER_SIDE_TOOLS`; ключи `TOOL_GENERATION_MODES` и `ARGS_DEGRADE_TOOLS` ⊆ `ALL_TOOL_NAMES`.
- **Валидация пула (границы, каждая — отдельный кейс):** валиден пул из 3 и из 10 вопросов; **невалидны** — 0/2/11 вопросов, вопрос с 1 и с 11 вариантами, `correctIndex = len(options)`, `correctIndex = -1`, `correctIndex = true` (**bool не является валидным int**), `question`/`option`/`explanation` сверх лимитов (1000/400/2000), лишний ключ, отсутствующее поле.
- **All-or-nothing:** пул из 5 вопросов, где невалиден только третий → ошибка на **весь** пул (частичного результата не существует).
- **Content-free сообщение:** текст ошибки `invalid_quiz` содержит путь поля и код, но **не содержит** ни текста вопроса, ни вариантов (проверяется подстрокой уникального значения из фикстуры).
- **Цена режима:** `chat_generation_credit_cost("study_learn")` = `CHAT_CREDIT_COST_STUDY_LEARN`; дефолт при незаданной env = **2**; неизвестный режим по-прежнему → цена `general`.
- **Валидатор положительности покрывает ЧЕТВЁРТОЕ поле (diff):** `CHAT_CREDIT_COST_STUDY_LEARN=0` и `=-5` → `chat_generation_credit_cost("study_learn")` возвращает **1** (кламп), а не `0`/отрицательное. Тот же кейс прогоняется для трёх соседних цен — **параметризованный тест по всем четырём полям**, чтобы следующее добавленное поле мимо валидатора роняло suite. Тест обязан **падать**, если новое поле не включено в общий field-валидатор (иначе режим тихо становится бесплатным на инстансе).
- **Whitelists режимов:** `generation_mode_for_message_step` и нормализация режима в клиентах провайдеров принимают все **четыре** значения (`study_learn` не схлопывается в `general` на этих границах).

### Unit — системный суффикс режима
- **Присутствует только в своём режиме (diff):** system-prompt, собранный для `study_learn`, содержит статичную строку-суффикс режима; для `general`/`research`/`reasoning` и для legacy-пути (`generation_backend="legacy"`) — **не** содержит. Тест падает, если суффикс добавлен безусловно или не добавлен вовсе (мёртвое объявление).
- **Порядок слоёв ([ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md)):** для сессии с workspace порядок в `system` — base-промт `assistant_mode` → суффикс `study_learn` → `workspace.instructions` (инструкции пользователя остаются **последними**). Проверяется по индексам вхождений подстрок.
- **Статичность / prompt-кэш:** два последовательных хода `study_learn` в одной сессии дают **побайтово равный** `system` (суффикс не несёт ни даты, ни счётчиков, ни содержимого хода) — по образцу проверки `time.now`-инструкции ([06-testing-strategy.md](../../06-testing-strategy.md#тест-кейсы-инструмента-timenow-adr-026)).
- **Доходит до провайдера (diff, wiring):** в аргументах фактического вызова fake-клиента на ходе `study_learn` параметр `system_prompt` содержит суффикс — проверка на рабочем пути (HTTP-вызов `/v1/chat/v2/run`), а не на хелпере сборки промта.

### Integration — сквозной путь (рабочий, не синтетика)
- **Пул доходит до клиента (diff):** `POST /v1/chat/v2/run` с `generationMode=study_learn`; fake отдаёт `tool_use quiz_generate` с валидным пулом из 4 вопросов, затем финальный текст → `200`, `status=assistant_message`, `quiz.questions` длины **4**, поля каждого вопроса совпадают с отданными байт-в-байт; `serverTools[]` содержит запись `toolName="quiz.generate"`, `status="completed"`. Тест падает, если поле `quiz` не заполняется на рабочем пути.
- **Инструмент реально предложен провайдеру (diff):** в аргументах, с которыми был вызван fake-клиент, tool-набор содержит `quiz.generate` при `study_learn` и **не** содержит при `general` в той же сессии (переключение режима между ходами одной сессии).
- **`assistantMessage` подавлен (diff):** fake отдаёт финальный ход с непустым текстом **и** квизом → в ответе `assistantMessage = null`, `quiz` непуст. Тест падает на реализации без подавления. Обратная сторона: ход **без** квиза с текстом → `assistantMessage` непуст (правило не переехало на не-квиз ходы).
- **История не изменена:** после такого хода `GET /v1/chats/{id}` отдаёт assistant-шаг **с** его текстом (подавление касается только run-проекции) и tool-шаг с `toolName="quiz.generate"` и `payload.result.questions` = тот же пул.
- **Цена режима списана:** `mode=credits`, активная подписка → баланс уменьшился ровно на `CHAT_CREDIT_COST_STUDY_LEARN` (тест с env, отличным от 1 и от цены прочих режимов, чтобы перепутанная цена была видна); `usage.generationMode = "study_learn"`; повтор того же `messageStepId` не списывает второй раз.

### Integration — degrade и guard
- **Невалидный пул НЕ роняет ход (diff):** fake отдаёт `quiz_generate` с `correctIndex`, равным `len(options)`, затем (после ошибки) валидный пул и финал → ответ `200` (**не** `422`), `quiz` непуст (второй пул), `serverTools[]` содержит **две** записи `quiz.generate`: `errored` (`summary="invalid_quiz"`) и `completed`. Тест падает, если `validate_tool_args` роняет ход `422`.
- **Границы degrade-сообщения (diff, три отдельных ассерта — [§границы](02-api-contracts.md#degrade-message--нормативные-границы)):**
  - **число записей:** пул с **8** независимыми нарушениями (напр. 8 вопросов с `correctIndex` вне диапазона) → в `message` не более **5** записей об ошибках;
  - **длина части:** нарушение в поле, чей путь содержит **длинное имя ключа, придуманное моделью** (лишний ключ ⇒ `extra_forbidden`, имя длиной ~1000 символов) → каждая запись ≤ **120** символов. Тест обязан **падать**, если срез по частям снят: ограничение одного лишь числа записей размер не удерживает;
  - **длина склейки:** тот же ввод → итоговый `message` ≤ **400** символов, причём подсказка про ограничения пула в сообщении **сохранена** (срез не должен выкосить инструкцию для модели);
  - **content-free сохраняется под всеми срезами:** ни в одной записи нет текста вопросов/вариантов из фикстуры (проверка подстрокой уникального значения) — срез не должен «случайно» протащить значение.
- **Контраст сохранён (соседняя ветка):** невалидные args **другого** инструмента (например `files.write` без `path`) по-прежнему дают `422`. Оба теста живут рядом — правило degrade не расползлось на весь `except`.
- **Упорство модели ограничено:** fake бесконечно отдаёт невалидный пул → ход завершается общим guard'ом `MAX_SERVER_TOOL_ROUNDS` (audit `max_server_tool_rounds_exceeded`, `502`), **кредит не списан**; отдельной квиз-ветки завершения нет.
- **Guard вне режима (diff):** `generationMode=general`, fake всё равно возвращает `quiz_generate` → ход **не падает** (`200`, не `502`), `quiz = null`, запись в `serverTools[]` со `status="errored"` и `summary="tool_not_available"`; пул НЕ исполнялся.
- **Приоритет отказов при пересечении (diff):** `generationMode=general`, fake возвращает `quiz_generate` с **невалидным** пулом (вне режима И невалидные args) → отдаётся **`tool_not_available`**, а `invalid_quiz` в ходе **не появляется** (`serverTools[]` содержит ровно одну запись `quiz.generate` со `summary="tool_not_available"`). Тест падает, если валидация args выполняется раньше проверки режима.
- **Контраст с `site.*`:** тот же сценарий для `site.write_file` в сессии без проекта по-прежнему даёт жёсткий отказ (`502`) — поведение двух guard'ов различается намеренно и покрыто обоими тестами.

### Integration — continuation, реплей, legacy, capabilities
- **Continuation наследует режим (diff, тихий класс ошибок):** ход `study_learn`, в котором модель вызвала client-side tool → `POST /v1/chat/v2/tool-result` → (а) tool-набор витка continuation содержит `quiz.generate`, (б) списана цена `study_learn`, (в) пул, отданный на этом витке, приходит в `quiz`. Тест обязан **падать**, если whitelist `generation_mode_for_message_step` не содержит `study_learn` (тогда режим молча деградирует к `general`: пропадает и цена, и инструмент).
- **Идемпотентный реплей возвращает квиз (diff, частный случай turn-scope):** повторный `/v1/chat/v2/tool-result` уже закрытого квиз-хода → `200`, `quiz` **непуст и равен** пулу исходного ответа, `assistantMessage = null` (подавление срабатывает и здесь), повторного списания нет. Тест падает на реализации, где реплей отдаёт `quiz=null` (тогда ретрай показал бы спойлер без карточек). **Контраст в том же тесте:** `serverTools` при этом реплее — **пустой `[]`** (индикатор выполнения не реконструируется). Ход без квиза на реплее → `quiz = null`.
- **Квиз в ноге `run`, финал в ноге `tool-result` — turn-scoped (diff, ГЛАВНЫЙ кейс гарантии §7):** ход `study_learn`, где fake в ОДНОМ assistant-шаге возвращает и `quiz_generate` с валидным пулом, и client-side `tool_use` (например `files.read`) → `/v1/chat/v2/run` отдаёт `status=tool_call`, непустой `quiz`, `assistantMessage=null`; затем `/v1/chat/v2/tool-result` с результатом инструмента → fake отдаёт финальный **текстовый** ответ (без нового вызова `quiz.generate`) → в ответе **`quiz` НЕПУСТ** (тот же пул хода) и **`assistantMessage = null`**. Тест обязан **падать** на per-call-семантике аккумулятора (там на второй ноге `quiz=null` → подавление не срабатывает → пользователь получает дубль вопросов и раскрытые ответы). Дополнительно: `serverTools` второй ноги **не** содержит `quiz.generate` (per-call индикатор, контраст с `quiz`).
- **Turn-scope не течёт на соседний ход:** следующий ход той же сессии в режиме `general` → `quiz = null` (фолбэк ограничен `messageStepId` текущего хода и предикатом `study_learn`).
- **Фолбэк не делает лишних запросов:** ход `general` (и legacy-ход) не выполняет выборку quiz-шагов — проверяется числом SQL-запросов на ход (как в тесте «без N+1» выше).
- **`quiz` при прочих статусах (ассерты — по пулу ХОДА, не по ноге):** policy-`blocked` → `quiz = null` (ход не создан, пула нет). `blocked`+`max_tokens` → `quiz` = **последний валидный пул этого хода (`messageStepId`), выданный на ЛЮБОЙ его ноге до обрыва** (в т.ч. пул из ноги `run`, когда обрыв случился на ноге `tool-result`-continuation); `null` — только если в ходе валидного пула не было вовсе. **Запрещён per-call-ассерт** вида «обрыв на continuation при пуле из ноги `run` → `quiz = null`»: он закрепил бы отклонённую per-call-семантику ([ADR-064 §Альтернативы](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) и разошёлся бы с кейсом «Квиз в ноге `run`, финал в ноге `tool-result`» выше. Обязательный кейс: пул выдан в ноге `run` → обрыв по `max_tokens` на ноге `tool-result` → `quiz` **непуст** (тот же пул), `assistantMessage` подавлен, кредит не списан.
- **Legacy не затронут (diff):** `POST /v1/chat/run` → (а) в tool-наборе, отданном fake-клиенту, `quiz.generate` **отсутствует**, (б) ответ содержит `quiz = null`, (в) списан **1** кредит, (г) `ChatRunRequest` с полем `generationMode` по-прежнему → `422`. Тест падает, если ось C считается по полю запроса, а не по эффективному режиму.
- **Capabilities — гейт объявления (diff, [ADR-065 §1](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)):**
  - **дефолт fail-closed:** `CHAT_ADVERTISED_GENERATION_MODES` не задан → в `generationModes[]` **три** элемента (`general`, `research`, `reasoning`), элемента `study_learn` **нет** (проверять именно **отсутствие**, а не `available:false`). Тест обязан **падать** на реализации с литеральным списком из четырёх элементов;
  - **объявление включается:** env `general,research,reasoning,study_learn` → четыре элемента, `study_learn` **последний**; env `study_learn` (один) → `general` всё равно присутствует (инвариант дефолтного режима);
  - **порядок канонический:** env `study_learn,general` → ответ идёт в порядке `general … study_learn`, а не в порядке env;
  - **мусор не роняет инстанс:** env `general,nope,research` → элементы `general`/`research`, `nope` проигнорирован, приложение стартует (WARNING, не исключение);
  - **гейт объявления ≠ гейт поведения (diff, ключевой):** при **дефолтном** env (режим не объявлен) `POST /v1/chat/v2/run` с `generationMode=study_learn` **работает** — `200`, квиз выдаётся, списана цена `study_learn`. Тест падает, если allowlist применён к валидации запроса;
  - **`available` не является гейтом:** у всех присутствующих элементов `available=true` при любом составе allowlist.
- **Наблюдаемость исходов (diff, [ADR-065 §3](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)):** после хода с успешным квизом растёт `quiz_generate_total{result="ok"}`; после degrade-раунда — `{result="invalid_quiz"}`; после вызова вне режима — `{result="tool_not_available"}`. Проверять **приращение** серии в `/metrics` (экспозиция), а не факт объявления счётчика: объявленная, но не инкрементируемая метрика — мёртвый артефакт. Метки не содержат текста квиза.
- **Ограничения — в JSON Schema (diff, [ADR-065 §4](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)):** `tool_input_schema("quiz.generate")` содержит `maxLength` **у элемента `options`** (`options.items.maxLength`), а также `minItems`/`maxItems` пула и `maxLength` `question`/`explanation`. Тест падает, если ограничение живёт только в кастомном валидаторе: модель тогда узнаёт о нарушении лишь из degrade-раунда — лишний upstream-вызов на ходу ценой 2 кредита.
- **Паритет объявлений структуры вопроса (diff, [ADR-065 §5](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)):** набор полей, типы, обязательность и границы у модели аргументов инструмента и у wire-модели поля `quiz` **совпадают** (сравнение JSON Schema обеих, а не глазами). При общем источнике объявления тест тривиально зелёный; при двух объявлениях — единственная защита от расхождения, которое иначе всплывёт как ошибка валидации на живом ходе.
- **Capabilities — цена и auth (diff):** при env, **объявляющем** `study_learn` (`CHAT_ADVERTISED_GENERATION_MODES=general,research,reasoning,study_learn` — иначе элемента в ответе нет, см. гейт объявления выше), `creditCost` элемента `study_learn` меняется вслед за `CHAT_CREDIT_COST_STUDY_LEARN` (тест с переопределённой env — доказывает, что значение берётся из того же моста, что и списание, а не захардкожено); `401` без JWT.

## Integration — история квиз-хода без спойлера (ADR-065 §2)

Нормативное покрытие read-time strip ([ADR-065 §2](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md); контракт — [chats/02-api-contracts.md §квиз-ход](../chats/02-api-contracts.md#quiz-strip-adr-065)). Сценарий-мотиватор: приложение выгружено ОС посреди квиза → холодный старт → загрузка истории.

- **Текст квиз-хода не отдаётся (diff, главный):** ход `study_learn`, где модель вернула и текст (содержащий формулировки вопросов и правильные ответы), и валидный пул → `GET /v1/chats/{id}` не содержит **ни одного** `type=text`-блока в assistant-шагах **этого** хода; уникальная подстрока из текста фикстуры в ответе **отсутствует**. Тест обязан **падать** на реализации без среза.
- **Пул остаётся доступен:** в том же ответе tool-шаг с `toolName="quiz.generate"` и `payload.result.questions` присутствует целиком — клиент может восстановить карточки после холодного старта.
- **Соседние ходы не затронуты:** обычный (не-квиз) ход той же сессии отдаёт свои `type=text`-блоки **байт-в-байт**; `tool_use`-блоки квиз-хода тоже сохраняются.
- **Ход с невалидным квизом — не квиз-ход:** если единственный `quiz.generate`-шаг хода несёт `error` (`invalid_quiz`) и валидного пула в ходе нет, текст assistant-шагов **отдаётся** (спойлера не было — не было и квиза).
- **Хранение и реплей не тронуты:** после отдачи истории `chat_steps.payload` в БД по-прежнему содержит текстовые блоки, а `_build_messages` реплеит их провайдеру (срез — на копии при сериализации, как [ADR-042](../../adr/ADR-042-hide-context-block-from-user-facing-history.md)).
- **Steps-view не обходит срез:** `GET /v1/chats/{id}/steps` для квиз-хода не отдаёт `summary`, построенный из текста assistant-шага (иначе спойлер возвращается вторым каналом).
- **Без N+1:** отдача истории сессии с несколькими квиз-ходами не добавляет запросов относительно сессии без квизов (множество квиз-ходов считается по уже загруженным шагам).
- **Контраст с [ADR-042](../../adr/ADR-042-hide-context-block-from-user-facing-history.md) покрыт обоими тестами:** user-шаг со `[Conversation settings…]` по-прежнему теряет **только ведущий блок** (остальной текст пользователя цел), а квиз-ход теряет **все** text-блоки assistant-шагов. Правила не переносятся друг на друга.

## Unit + Integration — локализация пресетов (ADR-049)
Реестр `chat/presets.py` и роутер `GET /v1/presets`. Ключи-хелперы: `preset_catalog(locale)`, резолвинг локали, config `presets_default_locale`.

**Реестр (`preset_catalog`, pure):**
- `preset_catalog("en")` и `preset_catalog("ru")` возвращают **7** пресетов в одинаковом порядке (declaration order); `id`/`icon` **идентичны** между локалями (не переводятся), `title`/`prompt` — различаются.
- Паритет наборов: каждый пресет имеет непустые `title["en"]`/`prompt["en"]` (канон обязателен) и `title["ru"]`/`prompt["ru"]`.
- **Per-field fallback:** неизвестная локаль (`preset_catalog("de")`) → EN-каталог (каждое поле = EN). (При частично заполненной локали недостающее поле берётся из EN.)
- Все 4 поля каждого элемента непусты; `id` уникален.

**Резолвинг локали (helper, чистый):**
- query `?locale=ru` → `ru`; `?locale=en` → `en`; `?locale=RU`/` ru ` (нормализация) → `ru`.
- явный `?locale=de` (вне набора) → **`422`** (`unsupported`), НЕ тихий fallback.
- нет query, `Accept-Language: ru-RU,en;q=0.8` → `ru`; `en-US` → `en`; `fr` (нет поддерживаемого) → следующий шаг (тихо).
- нет query, `Accept-Language` пуст/нераспознан + `PRESETS_DEFAULT_LOCALE=ru` → `ru`; без env → `en`.
- приоритет: query важнее `Accept-Language` важнее env важнее `en`.

**Config (`presets_default_locale`):**
- `PRESETS_DEFAULT_LOCALE=ru` → дефолт `ru`; не задан → `en`; вне набора (`PRESETS_DEFAULT_LOCALE=zz`) → graceful `en` (+ WARNING), НЕ исключение на старте.

**Роутер (`GET /v1/presets`, integration):**
- ответ содержит поле `locale` = фактически отданная локаль; при `?locale=ru` → `locale:"ru"` и русские `title`/`prompt`.
- без параметров и без env → `locale:"en"` + EN-тексты (обратная совместимость с ADR-035).
- `422` на `?locale=<вне набора>`; `401` без JWT; `429` rate-limit (как ADR-035).
- порядок элементов стабилен во всех локалях.

## E2E (AC-4)
- Полный tool-loop: run → tool_call → tool-result → tool_call → ... → assistant_message (≥2 итерации).
- **Server-side tool-loop continuation (BUG-5 регресс, live):** website-builder `site.*` multi-round tool-loop с реальным Claude → реконструкция диалога корректна (нет orphan tool_result, нет Anthropic 400/502). Покрывается live e2e website-builder после восстановления org Anthropic (см. memory/deployment-state).
- **Continuation с реалистичным anthropic id (BUG-4 регресс):** fake возвращает `tool_use.id = "toolu_..."`; на раунде continuation проверить, что отправленный в Anthropic `tool_result.tool_use_id` **точно равен** этому raw id (а не доменному UUID), и реплеенный assistant `tool_use.id` совпадает с ним → второй `messages.create` не падает с 400. Тест должен падать на старой реализации (`uuid4`-подмена).
