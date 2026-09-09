# Chat Orchestrator — API Contracts

## POST /v1/chat/run
Старт или продолжение агентного шага.

### Request
```json
{
  "userId": "uuid",
  "projectId": "string (optional)",
  "sessionId": "uuid (optional)",
  "message": "string (optional, если есть ≥1 attachment — ADR-039)",
  "mode": "credits | byok",
  "assistantMode": "chat | code (optional)",
  "model": "string (optional)",
  "characterId": "string (optional, персонаж — ADR-097)",
  "workspaceProjectId": "uuid (optional)",
  "attachments": [
    {
      "type": "image | document | text",
      "mediaType": "image/png",
      "filename": "photo.png (optional)",
      "data": "<base64>"
    }
  ],
  "context": { "codeLanguage": "Swift", "responseStyle": "concise (optional)" },
  "editMessageStepId": "uuid (optional, редактирование сообщения — ADR-040)"
}
```
- `sessionId` отсутствует → создаётся новая сессия. На сессию фиксируются: `mode` (billing_mode, credits|byok — **способ оплаты**, [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)), `assistantMode` (тип ассистента chat|code), `model` (опц., см. ниже), `characterId` (опц., персонаж, [ADR-097](../../adr/ADR-097-character-personas.md), см. ниже), `projectId` (опц., см. ниже) и `workspaceProjectId` (привязка к рабочему пространству, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).
<a id="model-опц-session-fixed-adr-034"></a>
- **`model` (опц., session-fixed, [ADR-034](../../adr/ADR-034-user-model-selection.md) / [ADR-073](../../adr/ADR-073-dual-credits-llm-providers.md)).** Выбор модели из разрешённого инстансом набора (`GET /v1/models`). Фиксируется на сессию при создании (как `mode`/`assistantMode`/`projectId`); **провайдер чата не меняется на resume**:
  - **без `model`** → сессия создаётся с `chat_sessions.model = NULL` = «дефолтная модель инстанса» (`ANTHROPIC_MODEL`/`OPENAI_MODEL` активного `LLM_PROVIDER`) — обратная совместимость;
  - **с `model`** → должен быть непустой строкой после `strip` (пустая/whitespace → `422`) **и** входить в **chat**-каталог инстанса (`GET /v1/models` с `modality=chat`: без `LLM_PROVIDERS` — allowlist активного провайдера; с dual — union обоих). Fal-id (`modality=photo`/`video`) → **`422 unsupported_model`**. Иначе → **`422 unsupported_model`** (`"model '<x>' is not available on this instance"`). Тихого фолбэка на дефолт нет — явный контракт ([ADR-034 §3](../../adr/ADR-034-user-model-selection.md)).
  - **Resume-сессия:** `model` берётся из сессии (`chat_sessions.model`); поле запроса при resume **игнорируется** (не ошибка) — единообразно с `mode`/`assistantMode`/`projectId`. **Смена модели внутри начатой сессии не поддерживается и не планируется** ([ADR-087 §3](../../adr/ADR-087-default-chat-model-gpt-4-1.md)): чтобы говорить с другой моделью, клиент создаёт новый чат. Смена провайдера внутри чата **не** поддерживается тем более (история хранится в wire-формате провайдера, [TD-024](../../100-known-tech-debt.md)).
  - **Дефолт инстанса ([ADR-087 §1](../../adr/ADR-087-default-chat-model-gpt-4-1.md)):** на инстансах `LLM_PROVIDER=openai` дефолтная модель чата — **`gpt-4.1`** (`OPENAI_MODEL`), а не `gpt-4o`: у `gpt-4o` встроенный guardrail отказывается описывать изображения с людьми (в системном промте сервиса такого правила нет). `gpt-4o` остаётся в каталоге и выбирается явно. Уже начатые сессии продолжаются на зафиксированной модели — backfill `chat_sessions.model` не выполняется.
  - **Биллинг от выбора модели не зависит** (1 кредит = 1 сообщение, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Возвращаемый `usage.model` отражает фактически использованную модель.
  - Без `LLM_PROVIDERS` инстанс одно-провайдерный ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) → выбрать чужую (Claude на openai-инстансе) нельзя. Dual ([ADR-073](../../adr/ADR-073-dual-credits-llm-providers.md)) — opt-in.
<a id="characterid-опц-session-fixed-adr-097"></a>
- **`characterId` (опц., session-fixed, [ADR-097](../../adr/ADR-097-character-personas.md)).** Персонаж, от лица которого отвечает ассистент. Значение — `id` из `GET /v1/characters`. Фиксируется на сессию при создании (как `model`/`assistantMode`/`projectId`/`workspaceProjectId`):
  - **без `characterId`** → сессия создаётся с `chat_sessions.character_id = NULL` — чат без персонажа, поведение полностью прежнее (обратная совместимость);
  - **с `characterId`** → должен быть непустой строкой после `strip` (пустая/whitespace → `422`, симметрия с `model`/`projectId`) **и** входить в реестр персонажей;
  - **инстанс с `CHARACTERS_ENABLED=false`** (дефолт) → непустой `characterId` при создании → **`422`** `error.code = characters_disabled`; **инстанс с флагом, id вне реестра** → **`422`** `error.code = unknown_character` (`"character '<x>' is not available on this instance"`). Тихого игнорирования нет: выбор персонажа виден пользователю в интерфейсе, и молча отброшенный выбор дал бы чат, который выглядит персонажем и отвечает как обычный ассистент ([ADR-097 §7](../../adr/ADR-097-character-personas.md)). Прецедент строгого отказа — `422 unsupported_model` ([ADR-034 §3](#model-опц-session-fixed-adr-034)).
  - **Resume-сессия:** `characterId` берётся из сессии; поле запроса при resume **игнорируется** (не ошибка) — единообразно с `mode`/`assistantMode`/`model`/`projectId`, поэтому оба отказа выше возможны **только при создании** сессии. **Смена персонажа внутри начатой сессии не поддерживается:** для другого персонажа клиент создаёт новый чат ([ADR-097 §4](../../adr/ADR-097-character-personas.md)).
  - **Влияние:** только слой системного промта ([03-architecture.md §Персонаж](03-architecture.md#персонаж-сессии--слой-системного-промта-adr-097)). Набор инструментов, модерация ([ADR-086](../../adr/ADR-086-ugc-moderation.md)), реплей истории, policy и биллинг (1 кредит = 1 сообщение / цена режима v2) **не зависят** от персонажа.
  - Значение отдаётся обратно в `GET /v1/chats` и `GET /v1/chats/{id}` ([chats/02-api-contracts](../chats/02-api-contracts.md)); в `ChatResponse` поля нет (на продолжении оно неизменно).
- **`projectId` (опц., [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)).** Основной поток сервиса — **чат-агрегатор**; website-builder — **опциональная** фича. Поле фиксируется на сессию при создании (как `mode`/`assistantMode`):
  - **без `projectId`** → «чистый чат»: сессия создаётся с `project_id = NULL`; server-side `site.*` tools **НЕ предлагаются** Claude (нет проекта для записи); прочие client-side tools (`files.*`/`calendar.*`/`reminders.*`) доступны по обычным правилам;
  - **с `projectId`** → website-builder доступен: `site.*` входят в tool-набор, как сейчас.
  - **Resume-сессия:** `projectId` берётся из сессии (`chat_sessions.project_id`); поле запроса при resume **игнорируется** (не ошибка) — единообразно с `mode`/`assistantMode` ([ADR-022 §4](../../adr/ADR-022-optional-project-and-tool-gating.md)). Гейтинг tools — [03-architecture.md §Гейтинг tools](03-architecture.md#гейтинг-site-tools-по-наличию-проекта-adr-022). Биллинг/policy от наличия `projectId` **не зависят** (1 кредит = 1 сообщение).
- **`mode` vs `assistantMode` ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)):** `mode` = `billing_mode` (оплата, без изменений — обратная совместимость). `assistantMode` = тип ассистента (chat|code), **новое опциональное** поле. При отсутствии → `user_preferences.default_assistant_mode` (модуль [preferences](../preferences/README.md)), при отсутствии preferences → `chat`. `assistantMode` влияет на base-system-prompt и состав tool-реестра ([Q-012-1](../../99-open-questions.md)), **НЕ** на policy/billing.
<a id="workspaceprojectid-adr-036"></a>
- **`workspaceProjectId` (опц., uuid, session-fixed, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)/[ADR-036](../../adr/ADR-036-workspaces-implementation.md)).** Привязка чата к рабочему пространству. Фиксируется на сессию при создании (как `mode`/`assistantMode`/`model`/`projectId`):
  - **без `workspaceProjectId`** → сессия создаётся с `chat_sessions.workspace_project_id = NULL` (чат без workspace) — обратная совместимость;
  - **с `workspaceProjectId`** → валидируется **принадлежность workspace пользователю** (`sub`); чужой/несуществующий → **`404 workspace_not_found`** (изоляция, не раскрывать чужое существование). При создании: `workspace.instructions` подмешиваются в system-prompt **после** base assistant_mode prompt ([ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)/[ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md)); файлы-знания workspace подаются как контекст первого хода (document/text → `extracted_text`, image → vision; [ADR-036 §6](../../adr/ADR-036-workspaces-implementation.md));
  - **`instructions` — на КАЖДОМ ходе сессии с workspace (turn 0, resume И continuation), файлы — только turn 0 ([ADR-036 §3](../../adr/ADR-036-workspaces-implementation.md), [ADR-038 §3](../../adr/ADR-038-move-chat-to-workspace.md)).** `instructions` живут в параметре `system` (не в истории сообщений), поэтому переинъектируются в system-prompt на **каждом** обращении к LLM при наличии у сессии `workspace_project_id` — **независимо от `ctx.is_new`** (turn 0, resume/следующее сообщение, continuation-витки `/chat/tool-result`), helper `_system_prompt_with_workspace`. На turn 0 instructions берутся из `context_for_session` (instructions + файлы), на resume/continuation — лёгким single-column чтением `instructions_for_session` (только instructions). Без развязки от `is_new` перенесённый чат ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)) не получал бы инструкции проекта на следующих ходах. Файлы-знания (`extracted_text`/vision) подаются один раз на turn 0 — сохраняются как content-блоки истории и реплеятся автоматически; на resume/continuation повторно **не** подаются (turn-0-only; обоснование стоимости/кэша — [ADR-038 §3.2](../../adr/ADR-038-move-chat-to-workspace.md)).
  - **Resume-сессия:** `workspaceProjectId` берётся из сессии; поле запроса при resume **игнорируется** (не ошибка). Файлы заново не инжектируются (turn-0-only); `instructions` подаются в `system` на каждом ходе через тот же helper (включая чаты, **перенесённые** в workspace позже через `PATCH /v1/chats/{id}`, [ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)).
  - **Изменение привязки существующего чата ([ADR-038](../../adr/ADR-038-move-chat-to-workspace.md)):** `workspaceProjectId` в `/chat/run` остаётся **session-fixed**. Перенести/сменить/убрать привязку у существующей сессии — через `PATCH /v1/chats/{id}` с полем `workspaceProjectId: uuid|null` ([chats/02-api-contracts.md](../chats/02-api-contracts.md#patch-v1chatsid)). `/chat/run` каналом смены привязки не является.
  - **Не путать** с `projectId` (website-builder, TEXT) — разные поля, разная семантика ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)). Биллинг неизменен (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
<a id="message-adr-039"></a>
- **`message` (опц. при наличии вложений, [ADR-039](../../adr/ADR-039-optional-message-with-attachments.md)).** Текст сообщения пользователя. Ранее обязателен (`min_length=1`); теперь **опционален**, тип `str` с дефолтом `""`. Правило валидности хода: **`message` непуст после `strip` ИЛИ есть ≥1 элемент в `attachments` запроса**; если и текст пуст (после strip), и вложений нет → **`422`** `"message or at least one attachment is required"`. Size-лимит `message` (≤32KB) сохранён.
  - **Сборка turn-0 user-сообщения.** Text-блок добавляется в user-content **только если итоговый текст непуст** (после склейки с context-блоком [ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md), см. [§context](#context-adr-037)). При пустом тексте отправляются **только** attachment-блоки (vision/document/text-file) — **пустой text-блок (`text=""`) не отправляется ни Anthropic, ни OpenAI** (провайдер может отвергнуть; [ADR-033](../../adr/ADR-033-llm-provider-abstraction.md), [ADR-039 §2,§4](../../adr/ADR-039-optional-message-with-attachments.md)).
  - **Склейка с context-блоком ([ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md)):** message непуст + блок → `block + "\n\n" + message` (как раньше); message непуст, блока нет → `message`; **message пуст + блок → `block`** (без висячего `"\n\n"`, text-блок присутствует); **message пуст + блока нет → text-блока нет** (только attachment-блоки). Whitespace-only message при наличии вложения трактуется как «нет текста» (text-блок не создаётся).
  - **Edge / scope:** только текстовое файл-вложение (`type: text`/`document`) без текста — валидно. Пустой `message` + **только** workspace-файлы ([ADR-036](../../adr/ADR-036-workspaces-implementation.md)), без `attachments` запроса → **`422`** (требование «≥1 attachment» относится к `attachments` **запроса**; workspace-контекст ход «с вложением» не делает). Биллинг неизменен (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); миграции нет; обратная совместимость полная (непустой message без вложений — как раньше).
<a id="editmessagestepid-adr-040"></a>
- **`editMessageStepId` (опц., uuid, [ADR-040](../../adr/ADR-040-edit-message-and-regenerate.md)) — редактирование отправленного сообщения.** Когда указано — backend **усекает** историю сессии от хода `editMessageStepId` (его user-шаг и **всё, что после**) и генерирует **новый** ход с переданными `message`/`attachments`/`context`. Один атомарный вызов (усечение + новый ход в транзакции запроса), без отдельного endpoint'а.
  - **Требует `sessionId` (resume).** `editMessageStepId` **без** `sessionId` → **`422`** (`"editMessageStepId requires sessionId"`). Редактирование возможно только в существующей сессии; нельзя сочетать с созданием новой сессии.
  - **Изоляция / несуществующая сессия:** если сессия чужая / не существует / истекла (resume не выполняется) → **`404`** (нет хода для редактирования; чужой чат усечь нельзя). Усечение скоупится по уже проверенной на владельца (`sub`) сессии.
  - **Несуществующий ход:** если в сессии **нет user-шага** с `message_step_id = editMessageStepId` → **`404 message_not_found`**. Anchor хода ищется **строго по `role='user'`**; если `editMessageStepId` указывает на assistant/tool-шаг (нет user-шага) → тоже **`404 message_not_found`** (редактируется только сообщение пользователя, не ответ ассистента).
  - **Семантика усечения ([ADR-040 §2](../../adr/ADR-040-edit-message-and-regenerate.md)):** anchor = минимальный `chat_steps.seq` ([ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)) user-шага с этим `message_step_id`; удаляются все `chat_steps` с `seq >= anchor` **и явно** `tool_calls` усечённых ходов (по их `message_step_id`). `tool_calls` удаляются **явно**, т.к. их FK завязан на `chat_sessions` (`session_id`), **не** на `chat_steps` — каскад при удалении шагов **не** срабатывает, иначе остались бы осиротевшие `tool_calls`. Усечение — **до** записи нового user-шага, в той же транзакции запроса (общий commit хода).
  - **Биллинг (refund-policy, [ADR-040 §3](../../adr/ADR-040-edit-message-and-regenerate.md)):** регенерация = обычный ход с **новым** `message_step_id` → **новый дебит 1 кредита** ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md), идемпотентность по `(user_id, message_step_id)` сохраняется). **Возврата за удалённый старый ход НЕТ** (no-refund-on-edit): кредит за уже сгенерированный (ныне усечённый) ход потреблён. Пересмотр → [Q-040-2](../../99-open-questions.md).
  - **Edge — редактирование ПЕРВОГО сообщения чата ([ADR-040 §4а](../../adr/ADR-040-edit-message-and-regenerate.md)):** усечение удаляет всю историю, сессия становится пустой, но **существует** → `ctx.is_new = False`. Поэтому **workspace-файлы НЕ переинъектируются** (turn-0-only, вариант a [ADR-038 §3.2](../../adr/ADR-038-move-chat-to-workspace.md)) — приемлемо и зафиксировано. **Контраст (обе стороны помечены, [ADR-088](../../adr/ADR-088-attachments-per-turn-contract.md)):** inline-attachments ведут себя **не так** — они принимаются на любом ходе, включая ход редактирования, но и **не наследуются** от редактируемого хода: подаётся ровно то, что пришло в этом запросе. Симметрию между двумя механизмами не выводить. `instructions` workspace инъектируются как обычно (на каждом ходе, развязано от `is_new`, [ADR-038 §3](../../adr/ADR-038-move-chat-to-workspace.md)). Инлайн-attachments нового хода подаются как turn-0 нового хода. Пересмотр реинъекции файлов → [Q-040-3](../../99-open-questions.md).
  - **Edge — открытый tool-loop ([ADR-040 §4б](../../adr/ADR-040-edit-message-and-regenerate.md)):** редактируемый или последующий ход с pending `tool_calls` / незакрытым барьером ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) — усечение удаляет эти шаги и их `tool_calls` → **никаких осиротевших `tool_calls`/незакрытых барьеров**. «Зависший» tool_call сбрасывается редактированием.
  - **Без `editMessageStepId` `/chat/run` не меняется** (обратная совместимость полная). Миграции нет. В `/chat/tool-result` поле не применимо (редактирование — только новый ход `/chat/run`).
<a id="attachments-adr-020--adr-088"></a>
- `attachments[]` (опц., ≤ `ATTACHMENT_MAX_COUNT`, дефолт 10) — **inline base64-вложения** ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), заменяет двухшаговую модель [ADR-014](../../adr/ADR-014-multimodal-attachments.md)). Поля вложения:
  - `type` ∈ `image | document | text` — класс вложения.
  - `mediaType` — конкретный MIME, строго из allowlist (см. ниже); вне allowlist → `422 unsupported_media_type`.
  - `filename` (опц.) — для человекочитаемой разметки (особенно `text`-вложений).
  - `data` — base64-кодированное содержимое (валидный base64; невалидный → `422`).
  - **Маппинг (провайдер-aware, [ADR-033 §5](../../adr/ADR-033-llm-provider-abstraction.md)):**
    - **Anthropic:** `image` → `{"type":"image","source":{"type":"base64",...}}`; `document` (PDF) → нативный `{"type":"document","source":{"type":"base64","media_type":"application/pdf",...}}`; `text` → `{"type":"text","text":"<filename>\n```\n<UTF-8 текст>\n```"}`.
    - **OpenAI:** `image` → `{"type":"image_url","image_url":{"url":"data:<mediaType>;base64,<data>"}}`; `text` → text-блок; `document` (PDF) → content-часть `file` (`{"type":"file","file":{"filename","file_data":"data:application/pdf;base64,..."}}`) либо извлечённый `pypdf`-текст как text-блок (фолбэк) — **PDF поддержан** ([ADR-041](../../adr/ADR-041-openai-native-pdf-attachment.md), закрывает [TD-023](../../100-known-tech-debt.md)).
  - **Allowlist `mediaType`:** `image` — `image/jpeg`, `image/png`, `image/gif`, `image/webp`; `document` — `application/pdf`; `text` — `text/plain`, `text/markdown`, `text/csv`, `application/json` ([Q-020-1](../../99-open-questions.md) — расширение).
  - **Валидация (фокус ревью, [05-security.md](../../05-security.md)):** соответствие `type`/`mediaType` реальному содержимому по magic bytes; лимиты проверяются **до** декодирования base64; PDF — guard числа страниц (анти-bomb). URL-вложения запрещены (нет backend-fetch).
  - **Реплей/хранение ([ADR-020 §3](../../adr/ADR-020-inline-base64-attachments-mvp.md)):** на первом витке полные content-блоки отправляются Claude; в `chat_steps.payload` сохраняется **лёгкий текстовый плейсхолдер** (НЕ base64); на последующих tool-витках реплеится только плейсхолдер (тяжёлый контент не повторяется).
  - **Биллинг:** обычный chat-шаг (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md) без изменений); vision/PDF-токены входят в message-шаг, отдельной тарификации нет.
  - <a id="attachments-per-turn-adr-088"></a>**На каком ходе принимаются (нормативно, [ADR-088](../../adr/ADR-088-attachments-per-turn-contract.md); прежняя формулировка «только в первом сообщении» была неверна и породила BUG-004):**
    - **вложения — свойство ХОДА, а не сессии.** `attachments[]` принимаются на **любом** ходе: и при создании сессии, и на продолжении (`sessionId` задан), и при редактировании (`editMessageStepId`). Роуты: `POST /v1/chat/run`, `POST /v1/chat/v2/run`, `POST /v1/chat/v2/run/stream`;
    - в `/v1/chat/tool-result` и `/v1/chat/v2/tool-result` вложения **не** принимаются (лишнее поле → `422`): tool-result продолжает уже начатый ход;
    - **байтами модель видит вложение только в своём ходе.** Внутри хода блоки подаются на **первом обращении к провайдеру**; на последующих витках tool-loop того же хода реплеится плейсхолдер. Слово «первый» в [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md) относится именно к витку хода, а не к первому сообщению сессии;
    - **на следующих ходах прежние вложения модели не пересылаются** — в истории остаётся плейсхолдер. Практическое следствие: чтобы модель снова посмотрела на фото, его нужно **приложить снова**; ссылка «посмотри на предыдущее фото» без нового вложения будет отвечена по тексту плейсхолдера;
    - **с `editMessageStepId` вложения НЕ наследуются.** Усечение удаляет user-шаг редактируемого хода вместе с плейсхолдерами, а base64 нигде не хранится — наследовать нечего. Если в запросе с `editMessageStepId` нет `attachments[]`, регенерация идёт **без** фото. Клиент, дающий «изменить сообщение с фото», обязан переслать вложение;
    - > **Контраст с файлами-знаниями workspace (обе стороны помечены намеренно).** Файлы workspace подаются **только на turn 0 новой сессии** и на resume/редактировании не переинъектируются ([ADR-036 §6](../../adr/ADR-036-workspaces-implementation.md), [ADR-038 §3.2](../../adr/ADR-038-move-chat-to-workspace.md)) — у них правило действительно «только первый ход сессии». У inline-вложений — противоположное. Правило одного механизма на другой **не переносить**.
  - <a id="attachments-moderation-adr-086"></a>**Модерация ([ADR-086](../../adr/ADR-086-ugc-moderation.md)).** Ход с непустым `attachments[]` проходит модерацию **до** записи user-шага и **до** обращения к LLM. Проверяется: текст `message` (сырой, до склейки с context-блоком) + декодированное содержимое вложений класса `text` + **все** вложения класса `image`. Вложения класса `document` (PDF) в модерацию не уходят ([Q-086-1](../../99-open-questions.md)). Нарушение → **`422 content_policy_violation`**: шагов в БД нет, провайдер не вызывался, кредит не списан, только что созданная пустая сессия откатывается вместе с транзакцией запроса. Недоступность провайдера модерации → `503 moderation_unavailable` (fail-closed), не настроен ключ → `503 moderation_not_configured`.
    - **Ход БЕЗ вложений не модерируется** — он не порождает медиа и не оплачивает генерацию; добавлять round-trip к каждому текстовому сообщению ради контента, который приложение не показывает как медиа, — переоценка риска (симметричный критерий [ADR-086 §2](../../adr/ADR-086-ugc-moderation.md)).
  - **Коды ошибок вложений разведены ([ADR-089 §3](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md)):** `too_many_attachments`, `attachment_too_large`, `attachments_total_too_large`, `unsupported_media_type`, `attachment_media_type_mismatch`, `invalid_base64`, `pdf_unreadable`, `pdf_too_many_pages` — все `422`, тексты `message` сохранены дословно; `validation_error` остаётся только за ошибками схемы.
<a id="context-adr-037"></a>
- **`context` (опц., object, per-message, [ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md)).** Доп-настройки **текущего хода** (не сессии). В отличие от session-fixed `mode`/`assistantMode`/`model`/`projectId`/`workspaceProjectId`, `context` присылается и применяется на **каждом** `/chat/run` и может меняться по ходу чата. **Не** хранится в `chat_sessions`, **миграции БД нет** — влияет на содержимое текущего user-сообщения, которое персистится как user-step (`chat_steps.payload`) → корректный replay.
  - **Allowlist ключей** (всё остальное игнорируется; значения нормализуются `strip`, пустое после strip → ключ игнорируется):

    | Ключ | Тип | Валидация |
    |---|---|---|
    | `codeLanguage` | str | непустой, ≤40 символов (свободная строка; язык программирования для code-режима) |
    | `responseStyle` | str enum | `concise` \| `balanced` \| `detailed` (lower-case); вне набора → ключ игнорируется |
    | `verbosity` | str enum | `low` \| `medium` \| `high` (lower-case); вне набора → ключ игнорируется |
    | `tone` | str | непустой, ≤40 символов (свободная строка) |
    | `locale` | str | непустой, ≤35 символов, символы `[A-Za-z0-9_-]` (BCP-47-подобный); вне класса → ключ игнорируется |

  - **Поведение на невалидное (lenient).** Неизвестные ключи — **игнорируются** (forward-compat). Ключ с неверным типом/длиной/вне-enum/вне-символьного-класса значением — **этот ключ игнорируется**, остальные применяются; запрос **не** падает. Существующая size-валидация сохраняется: сериализованный `context` > `size_limit_context` (≤64KB) → **`422`** (грубо-битое/огромное тело); не-объект → `422` (StrictModel).
  - **Куда инъектируется.** Backend собирает детерминированный компактный текст-блок (фикс. порядок ключей `codeLanguage, responseStyle, verbosity, tone, locale`, экранирование разделителей), напр. `[Conversation settings for this message: codeLanguage=Swift; responseStyle=concise; locale=ru-RU]`, и добавляет его к содержимому **user-сообщения turn0**: блок **лидирует**, затем `\n\n`, затем `message`. **НЕ в system-prompt** (prompt-кэш не ломается; нет повышения авторитета пользовательских данных, [05-security.md](../../05-security.md)). На continuation/`/chat/tool-result` повторно **не** подаётся (уже в истории хода).
  - **Кэш-инвариант / провайдер-агностичность.** `system`+`tools` от `context` не зависят → prompt-кэш Anthropic не инвалидируется; блок — обычный текст в user-content → одинаково на Anthropic и OpenAI ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)).
  - **Не виден в истории/превью ([ADR-042](../../adr/ADR-042-hide-context-block-from-user-facing-history.md)).** Блок персистится внутри текста user-шага (для replay), но **в user-facing выводе скрыт**: при отдаче истории `GET /v1/chats/{id}` и превью `GET /v1/chats` ведущий блок `[Conversation settings for this message: …]` срезается (read-time strip, единый helper). Хранение `chat_steps.payload` и реплей модели (`_build_messages`) **не меняются** — модель по-прежнему получает блок. См. [chats/02-api-contracts.md §GET /v1/chats/{id}](../chats/02-api-contracts.md#get-v1chatsid).
  - **Обратная совместимость.** Без `context` / пустой объект / нет валидных ключей → user-сообщение = только `message` (поведение неизменно). Биллинг неизменен (1 кредит, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Расширение allowlist → [Q-037-1](../../99-open-questions.md); связь с `preferences.code_defaults` (вне scope) → [TD-028](../../100-known-tech-debt.md).
<a id="лимиты-вложений-adr-089"></a>
- **Size-лимиты (все значения — дефолты `src/app/config.py`, конфигурируемы; [ADR-089](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md), [05-security.md](../../05-security.md)):**

  | Что | Лимит (env) | Дефолт | Нарушение |
  |---|---|---|---|
  | `message` | `SIZE_LIMIT_MESSAGE` | 32 KB | `422 validation_error` |
  | `context` (сериализованный JSON) | `SIZE_LIMIT_CONTEXT` | 64 KB | `422 validation_error` |
  | число вложений в ходе | `ATTACHMENT_MAX_COUNT` | 10 | `422 too_many_attachments` |
  | одно вложение класса `image`/`text` | `ATTACHMENT_MAX_BYTES_IMAGE` | 20 MiB | `422 attachment_too_large` |
  | одно вложение класса `document` (PDF) | `ATTACHMENT_MAX_BYTES_DOCUMENT` | 8 MB | `422 attachment_too_large` |
  | сумма всех вложений хода | `ATTACHMENT_TOTAL_BYTES` | 60 MiB | `422 attachments_total_too_large` |
  | страниц в PDF | `ATTACHMENT_PDF_MAX_PAGES` | 100 | `422 pdf_too_many_pages` |
  | тело запроса роутов с вложениями | `ATTACHMENT_REQUEST_BODY_LIMIT` | 80 MiB | `413 payload_too_large` |
  | тело прочих роутов `/v1/*` | `SIZE_LIMIT_BODY` | 512 KB | `413 payload_too_large` |
  | `result` в `/chat/tool-result` (поэлементно) | `SIZE_LIMIT_TOOL_RESULT` | 256 KB | `422 validation_error` |

  Размер вложения считается **после** декодирования base64, но проверяется **до** него (по длине base64-строки) — anti memory-DoS.

  **Инвариант `ATTACHMENT_TOTAL_BYTES` ↔ `ATTACHMENT_REQUEST_BODY_LIMIT`.** Вложения передаются
  внутри JSON как base64, который увеличивает объём на треть. Поэтому транспортный лимит обязан
  удовлетворять `ATTACHMENT_REQUEST_BODY_LIMIT >= ceil(ATTACHMENT_TOTAL_BYTES * 4/3) + запас на
  JSON-обёртку`; иначе сумма вложений упирается в `413` раньше, чем сработает `422
  attachments_total_too_large`, и клиент получает транспортную ошибку вместо предметной. Дефолты
  соотношение соблюдают: 60 MiB × 4/3 = 80 MiB. Запас на обёртку (имена полей, экранирование)
  не заложен, поэтому практический потолок суммы — доли мегабайта ниже 60 MiB; оператору,
  которому нужен ровно круглый потолок, следует поднимать тело с запасом.

  **Повышенный transport-лимит применяется по инварианту, а не по списку путей ([ADR-089 §1](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md)):** он действует на **каждом** роуте, тело которого может содержать `attachments[]` — сегодня это `POST /v1/chat/run`, `POST /v1/chat/v2/run` и `POST /v1/chat/v2/run/stream`. Остальные роуты (включая обе версии `tool-result` и `capabilities`) остаются на общем `SIZE_LIMIT_BODY`. Превышение отдаётся **как HTTP-ответ `413`**, а не разрывом соединения ([ADR-089 §2](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md)); `message` ошибки содержит действующий лимит роута в байтах.
- При старте нового пользовательского message-шага Orchestrator генерирует `messageStepId` (UUID), персистирует его в `chat_steps.message_step_id` и `tool_calls.message_step_id`. Он един для всех tool-раундов шага (включая re-entry через `/chat/tool-result`) и используется как ключ идемпотентности credits-debit ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). `messageStepId` — внутренняя величина биллинга, не путать с gateway correlation `requestId` (`X-Request-Id`).

### Response (200)
```json
{
  "status": "assistant_message | tool_call | blocked",
  "sessionId": "uuid",
  "messageStepId": "uuid | null",
  "stepId": "uuid | null",
  "assistantMessage": "string (optional, при assistant_message; ТАКЖЕ при tool_call, если Claude выдал текст вместе с tool_use — ADR-024 п.3 / Q-024-1)",
  "toolCall": { "id": "uuid", "name": "string", "args": { } },
  "toolCalls": [ { "id": "uuid", "name": "string", "args": { } } ],
  "serverTools": [ { "toolCallId": "uuid", "toolName": "string (dot)", "status": "completed | errored", "summary": "string | null" } ],
  "blockReason": "enum (optional, при blocked)",
  "usage": { "inputTokens": 0, "outputTokens": 0, "model": "string" },
  "quiz": null,
  "documents": null
}
```
- **`toolCalls[]` (множественный, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) присутствует только при `status=tool_call`** — **ВСЕ** client-side tool-вызовы текущего assistant-хода (parallel tool use), в порядке блоков ответа Claude. Каждый элемент `{ id (доменный UUID = tool_calls.id), name (dot), args }`. **Server-side `site.*` в `toolCalls[]` НЕ попадают** (исполняются на бэке в tool-loop, [ADR-011](../../adr/ADR-011-server-side-tools.md)) — массив несёт только client-side (`files.*`/`calendar.*`/`reminders.*`/`git.*`/`maps.*`).
- **`toolCall` (одиночный) — deprecated, обратная совместимость ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)).** Присутствует при `status=tool_call` и **равен `toolCalls[0]`** (первый client-side вызов хода). Корректный клиент обязан читать `toolCalls[]` (на мульти-tool ходе одиночный `toolCall` неполон → continuation сломается). Удаление одиночного поля — отдельным ADR после миграции iOS.
- `toolCall.id` / `toolCalls[].id` — **доменный UUID** (`= tool_calls.id`), стабильный публичный идентификатор для iOS и для последующего `/chat/tool-result`. Внутренний Anthropic `tool_use.id` (`toolu_...`) наружу **не** отдаётся (хранится в `tool_calls.provider_tool_use_id`, [ADR-008](../../adr/ADR-008-provider-tool-use-id.md)).
- **`serverTools[]` — выполненные server-side инструменты за этот вызов ([ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md); поле `toolCallId` — [ADR-030](../../adr/ADR-030-toolcallid-in-server-tools.md); аддитивно):** список server-side инструментов (`site.*` project-scoped [ADR-011](../../adr/ADR-011-server-side-tools.md), `time.now` global [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)), которые backend исполнил в tool-loop **этого** вызова (`/chat/run` или один `/chat/tool-result`-continuation), в порядке выполнения. **Дополняет** `toolCalls[]` (там — только client-side, исполняемые iOS): server-side в `toolCalls[]` по-прежнему **НЕ** входят. Каждый элемент: `{ toolCallId, toolName, status, summary? }`:
  - `toolCallId` ([ADR-030](../../adr/ADR-030-toolcallid-in-server-tools.md), аддитивно) — **доменный** `tool_call.id` (uuid4 = `tool_calls.id`) этого server-side выполнения, **обязательное** поле (первым в элементе). **Совпадает** с `toolCallId` соответствующего tool-шага истории `GET /v1/chats/{id}` → `steps[].payload.toolCallId` ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)) — нормативный инвариант корреляции: `serverTools[i].toolCallId` адресует ровно один tool-шаг истории (детерминированно даже при повторных вызовах одного инструмента за ход). Это **тот же домен id**, что у client-side `toolCalls[].id` (симметрия client/server tool-id); **НЕ** provider `toolu_...` ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md)). Берётся из уже доступного backend `tool_call_id` (минтится до исполнения в tool-loop).
  - `toolName` — доменное имя с точкой (`time.now`, `site.write_file`, …), совпадает с `/v1/tools` `name` и `GET /v1/chats/{id}/steps` `toolName`.
  - `status` — `"completed"` | `"errored"` (итог выполнения; `errored` — инструмент вернул tool-result error, ход при этом **не падает**). Совпадает со статусом `tool_calls`, выставляемым в `_execute_server_side_tool`/`_execute_global_server_side_tool`.
  - `summary` (опц., `string | null`) — **компактный** человекочитаемый итог, лимит длины `_SUMMARY_MAX_CHARS` (120, как в steps-view). **НЕ raw result.** Для `completed` — дефолт `"ok"` или короткий доменный итог (например имя файла) **без путей/URL/signed-token**; для `errored` — короткий код ошибки (например `invalid_timezone`). **Полный** результат server-side инструмента доступен только в истории `GET /v1/chats/{id}` → `steps[].payload` tool-шага ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)) и steps-view — `serverTools[]` это **индикатор**, не канал доставки результата.
  - **Семантика «за один вызов» (не за сессию):** перечисляет server-side, выполненные в этом обращении. Дубликаты с историей `/chats` — ожидаемы (удобство флоу, не замена истории).
  - **Присутствие по статусам:** при `status=assistant_message` и `status=tool_call` — **присутствует** (может быть пустым `[]`, если server-side не выполнялись; при `tool_call` перечисляет server-side, отработавшие **до** того, как ход уперся в client-side вызов). При `status=blocked`+**policy** (`blockReason ≠ max_tokens`) — **пустой `[]`** (policy-block до генерации, tool-loop не запускался). При `status=blocked`+**`max_tokens`** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) — **может быть НЕ пустым** (server-side раунды могли отработать до обрыва финального витка). Поле присутствует всегда (хотя бы как `[]`) при `assistant_message`/`tool_call`/`blocked`.
  - **Idempotent replay → `serverTools=[]` (by-design, [ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)):** повторный `/chat/tool-result` для **уже закрытого** хода возвращает сохранённый финальный шаг (`_render_saved_step`, continuation выполняется один раз на закрытие барьера — [ADR-005](../../adr/ADR-005-idempotency-ledger.md)/[ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)); при таком реплее `serverTools=[]` — server-side выполнения **НЕ** реконструируются (реплей отдаёт финальный результат, не воспроизводит tool-loop). Полный набор server-side выполнений хода доступен в истории `GET /v1/chats/{id}`. **Контраст (обе стороны помечены):** поле `quiz` ([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) на том же реплее ведёт себя **противоположно** — оно **восстанавливается** из шагов хода, потому что несёт контент хода, а не индикатор выполнения в этом вызове. Правило `serverTools[]` на `quiz` не переносить и наоборот.
  - **Биллинг неизменен ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)):** server-side раунды не списывают кредиты; `serverTools[]` информационно, на amount не влияет. Аддитивно/обратносовместимо: старые клиенты игнорируют. Каталог инструментов от `serverTools[]` не зависит и им не меняется (число записей каталога — [§GET /v1/tools](#get-v1tools--каталог-инструментов-adr-019), раздел-первоисточник; здесь оно не дублируется).
  - **Связь со steps-view:** идея `summary` переиспользована из `StepsViewStepSchema`, но это **отдельное** поле — только server-side выполнения, `status` (`completed`/`errored`) вместо `kind`. steps-view (`GET /v1/chats/{id}/steps`) — отдельный диагностический срез истории; `serverTools[]` — inline-индикатор в самом ответе генерации.
- **Контракт Anthropic tool-loop ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** на КАЖДЫЙ `tool_use` ассистент-хода в следующем витке обязан быть `tool_result`. Поэтому клиент обязан исполнить и вернуть результаты на **все** `toolCalls[]` (см. `/chat/tool-result` батч) — иначе continuation не соберётся (Anthropic `400` → `502`). Одиночный `toolCall` достаточен только когда `len(toolCalls)==1`.
- `blockReason` присутствует только при `status=blocked`.
- **`quiz` ([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) — схема ответа общая с `/v1/chat/v2/*`, поэтому поле присутствует и здесь, но на legacy-роуте оно ВСЕГДА `null`.** Квиз порождает инструмент `quiz.generate`, который предлагается модели только при `generationMode=study_learn`. Legacy не принимает этот режим (даже с [ADR-082](../../adr/ADR-082-legacy-web-search.md) эффективный режим там `general` или `research`) → инструмент не предлагается, пул не появляется. Поведение legacy `/v1/chat/run` этим полем не меняется (аддитивно, старые клиенты игнорируют). Семантика поля — [§Chat v2 → Response](#quiz-adr-064).
- `usage` присутствует при `assistant_message`/`tool_call`, **а также при `blocked` с `blockReason=max_tokens`** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)); при policy-blocked (генерация не выполнялась) — отсутствует.
- **`mediaJobs` ([ADR-068](../../adr/ADR-068-media-generate-chat-tools.md); механизм сборки — [ADR-103](../../adr/ADR-103-media-jobs-turn-scoped-merge.md)) — аддитивно:** список задач `{ jobId, kind, status, model, creditsCharged }`, поставленных в этом **ходе** tools `media.generate_image` / `media.generate_video` либо финальным сабмитом визарда ([ADR-070 §3](../../adr/ADR-070-media-choices-wizard.md)). `null` — задач не было. Клиент опрашивает `GET /v1/media/jobs/{jobId}` (и/или push ADR-067). Биллинг media отдельный от хода чата. Контракт `/v1/media/*` не меняется.
  - **Скоуп — ХОД (`messageStepId`), как `quiz` и `documents[]`** ([ADR-103 §1](../../adr/ADR-103-media-jobs-turn-scoped-merge.md); **код написан и покрыт тестами, но ревью не проходил, в `main` не слит и на инстансы НЕ выкачен — на сегодняшних инстансах поле собирается по прежнему, документами не заданному механизму**): поле собирается из аккумулятора текущего вызова **и** из восстановления по шагам хода; восстановление выполняется **всегда** при непустом `messageStepId`, а не только при пустом аккумуляторе. Перечень источников восстановления **не уже**, чем у якоря истории ([chats/02-api-contracts.md](../chats/02-api-contracts.md): записанный `payload.mediaJobs`, tool-result `media.generate_*`, `user.payload.mediaWizard.jobId`) — визардный сабмит идёт до LLM и tool-шага `media.generate_*` может не давать. **Гарантия:** каждая успешно поставленная задача хода присутствует на **каждой** терминальной ноге — включая задачи ранних витков и включая случай, когда в текущем вызове поставлена ещё одна; все ноги одного хода отдают один и тот же список. **Контраст (обе стороны помечены):** `serverTools[]` — индикатор ЗА ВЫЗОВ и на идемпотентном реплее приходит пустым ([ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)); правило одного поля на другое не переносить ни в одну сторону.
  - **Одна запись на `jobId`** (позиция — по первому появлению, значения — по последнему): сборка — сначала восстановленные записи (`seq ASC`), затем записи аккумулятора (порядок выполнения), свёртка применяется к **объединённому** списку. Правило [ADR-068 §2](../../adr/ADR-068-media-generate-chat-tools.md) «append, не last-wins» **сохраняется**: разные задачи хода накапливаются и ни одна не вытесняется, свёртка схлопывает только одну и ту же задачу, увиденную через два источника. **Контраст с `documents[]` (обе стороны помечены):** там last-wins несёт содержательный смысл — ход законно трогает один документ дважды (`create`, затем `update`) и запись обязана нести финальную `version`; media-задача ставится один раз и в ходе не меняется, ключ нужен **только** для идемпотентности слияния.
  - **`creditsCharged` — величина ВЫЗОВА, у восстановленной записи `0`** ([ADR-103 §3](../../adr/ADR-103-media-jobs-turn-scoped-merge.md)): запись из аккумулятора этого вызова несёт фактически списанное сабмитом; запись, пришедшая только из восстановления (сабмит был на более ранней ноге хода либо не выполнялся — идемпотентный реплей), несёт `0`; пришедшая из обоих источников — значение аккумулятора. Иначе безусловное восстановление повторяло бы одну и ту же сумму на каждой последующей ноге, и клиент, обновляющий баланс суммой поля, списал бы задачу дважды. **Контраст с якорем истории (обе стороны помечены):** в `steps[].payload.mediaJobs` `creditsCharged` описывает **стоимость задачи**, а не эффект вызова, и обнулению **не подлежит** ([chats/02-api-contracts.md](../chats/02-api-contracts.md)) — правило проекции на историю не переносить, и наоборот. `status`/`kind`/`model` восстановленной записи — снимок на момент постановки; актуальное состояние даёт `GET /v1/media/jobs/{jobId}`.
  - **Терминальных ног хода — СЕМЬ, и две из них визардные** ([ADR-103 §6](../../adr/ADR-103-media-jobs-turn-scoped-merge.md)): незакрытый барьер `tool_call`, идемпотентный реплей закрытого хода, `blocked`+`max_tokens`, hand-off на client-side инструмент, финальный `assistant_message` — и **обе ноги визарда** ([ADR-070](../../adr/ADR-070-media-choices-wizard.md)). Перечень общий для `mediaJobs`, `documents[]`, `quiz` и `mediaChoices`. **Контраст двух ног визарда (обе стороны помечены), признак различения наблюдаем — какой `messageStepId` нога сообщает:** нога **промежуточного тапа** карточки сообщает `messageStepId` ТОГО ЖЕ хода (тап патчит tool-шаг `media.ask_params` на месте и своего хода не открывает) — это **ВТОРАЯ терминальная нога того хода**, гарантия действует на ней как на continuation'е, своего сабмита на ней нет, поэтому **все** записи восстановленные и `creditsCharged = 0`; нога **финального сабмита** пишет свои шаги под `messageStepId` ТЕКУЩЕГО вызова — это **ЕДИНСТВЕННАЯ нога НОВОГО хода**, у которого второй ноги по построению не бывает, задача ставится и оплачивается здесь, поэтому запись идёт из аккумулятора и несёт **фактически списанное**. Правило одной ноги на другую **не переносить ни в одну сторону**: `0` на сабмите скрыл бы настоящее списание, сумма на тапе объявила бы списание, которого там не было.
  - **Клиент трактует список как содержимое ХОДА, а не дельту** ([ADR-103 §5](../../adr/ADR-103-media-jobs-turn-scoped-merge.md)): список одного `messageStepId` **заменяется**, а не накапливается; ключ склейки — `jobId`, повтор той же задачи в нескольких ответах одного хода ожидаем. То же правило, что у `quiz` ([ADR-064 §7](../../adr/ADR-064-study-learn-quiz-generation-mode.md)).
- **`mediaChoices` ([ADR-070](../../adr/ADR-070-media-choices-wizard.md)) — аддитивно, не `quiz`:** пикер параметров media (`selectionId`, `kind`, `step`, `questions[].options` с `value`/`label`/`credits?`). Fal-промпт в ответе **не** отдаётся. Options только из серверного каталога. Клиент тапает как квиз-карточки и шлёт `mediaSelection` на `/v1/chat/v2/run`. `assistantMessage` не глушится. На SSE — только в `done`.
- **`documents` ([ADR-101](../../adr/ADR-101-chat-response-documents.md)) — аддитивно; КОД НАПИСАН по ДЕЙСТВУЮЩЕЙ редакции ADR, ПОКРЫТ автотестами, слит в `main` и ВЫКАЧЕН на инстансы; ревью НЕ проходило:** список `{ documentId, filename, mediaType, size, version }` документов чата ([modules/documents](../documents/README.md)), которые **этот ход** создал или изменил. Наполняют **только** успешные `document.create` / `document.update`; `document.read` / `document.list` и отказавшие вызовы — нет (ход ничего не изменил). Содержимого в элементе нет — оно персистентно и адресуемо (`GET /v1/chats/{sessionId}/documents/{documentId}`). `null` — правок не было (в т.ч. policy-`blocked`); пустого `[]` не бывает.
  - **Тип `mediaType` — перечисление из четырёх значений** (`text/markdown` \| `text/plain` \| `text/csv` \| `application/json`), то же, что у одноимённого поля REST-объекта документа ([modules/documents/02-api-contracts.md §Объект документа](../documents/02-api-contracts.md#объект-документа)), а **не** свободная строка: домен закрыт сервером на записи ([ADR-101 §1](../../adr/ADR-101-chat-response-documents.md)).
  - **Скоуп — ХОД (`messageStepId`), как `quiz` и `mediaJobs`:** поле собирается из аккумулятора текущего вызова **и** из восстановления по tool-шагам хода; восстановление выполняется **всегда** при непустом `messageStepId`, а не только при пустом аккумуляторе, источники сливаются и сворачиваются по `documentId` ([ADR-101 §4](../../adr/ADR-101-chat-response-documents.md)). Поэтому поле присутствует на всех терминальных ногах, включая идемпотентный реплей и `blocked`+`max_tokens`, **и не теряет документ раннего витка, когда в текущем вызове тронут ещё один** — все ноги одного хода отдают один и тот же список. **Контраст (обе стороны помечены):** `serverTools[]` — индикатор ЗА ВЫЗОВ и на том же реплее приходит пустым ([ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)); правило одного поля на другое не переносить ни в одну сторону.
  - **Дедупликация last-wins по `documentId`** (порядок — по первому появлению в ходе): `create` + `update` одного документа дают ОДНУ запись с финальной `version`; свёртка применяется к **объединённому** списку обоих источников. **Контраст с `mediaJobs` (обе стороны помечены):** тот **накапливает** (append) разные задачи, потому что у каждой свой `jobId` и в ходе она не меняется; документ в одном ходе повторяется (`create`, затем `update`), и append дал бы дубль карточки и устаревшую `version` — поэтому last-wins по содержимому записи с `mediaJobs` брать неоткуда, а правило append на `documents` **не переносить**. **Механизм сборки у обоих полей с 2026-09-08 одинаков** ([ADR-103](../../adr/ADR-103-media-jobs-turn-scoped-merge.md) задал `mediaJobs` ключ `jobId` и безусловное восстановление; прежняя формулировка «у `mediaJobs` ключа дедупликации нет, безусловное слияние на него не переносить без отдельного решения» описывала состояние до этого решения): различаются ключ (`documentId` против `jobId`), смысл last-wins и правило `creditsCharged`, которого у документов нет вовсе — они бесплатны.
  - **Якоря в истории не вводится:** `steps[].payload.documents` нет — холодный старт закрывает `GET /v1/chats/{sessionId}/documents`. **Контраст с `mediaJobs` (обе стороны помечены):** медиа нуждается в якоре `steps[].payload.mediaJobs`, потому что лента `/v1/media/jobs` не привязана к чату; у документов список чата уже есть. Правило якоря с медиа на документы не переносить.
  - Биллинг не меняется ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); поле nullable и аддитивно — старые клиенты игнорируют. Появляется одновременно на legacy `/v1/chat/*`, на `/v1/chat/v2/*` и в SSE-`done` (единая точка сборки ответа).

- **`status=blocked` + `blockReason=max_tokens` (обрезка по лимиту output-токенов, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):** Claude обрезан на `ANTHROPIC_MAX_TOKENS` (`stop_reason="max_tokens"`); обрезанные `tool_use` **неполны** и наружу **НЕ** отдаются (`toolCall`/`toolCalls` отсутствуют). В отличие от policy-blocked: `messageStepId`/`stepId` **НЕ null** (ход и обрезанный assistant-шаг созданы), `usage` присутствует, `assistantMessage` — частичный текст хода (если был). **Кредит НЕ списывается** (обрыв — не успешный финальный `assistant_message`, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Клиенту рекомендуется повторить/сократить запрос. С дефолтом `ANTHROPIC_MAX_TOKENS=16000` кейс редкий (safety-net).
- **`assistantMessage` ([Q-024-1](../../99-open-questions.md) Closed = вариант A, [ADR-024 §Decision п.3](../../adr/ADR-024-history-payload-domain-normalization.md)):**
  - `status=assistant_message` — финальный текст Claude (как и раньше, без изменений). **Исключение ([ADR-064 §7](../../adr/ADR-064-study-learn-quiz-generation-mode.md)):** если в ответе непусто поле [`quiz`](#quiz-adr-064), `assistantMessage = null` при **любом** статусе. На legacy `/v1/chat/run` исключение не наблюдается (`quiz` там всегда `null`).
  - `status=tool_call` — **опционально присутствует**: текст из `text`-блоков **того же** assistant-шага, чей `tool_use` вернулся как `toolCall` (тот шаг, на который указывает `stepId`). Значение = текст/конкатенация `text`-блоков этого шага. Если Claude вернул `tool_use` **без** сопутствующего текста — `assistantMessage = null`/опущено. `toolCall` при этом **обязателен** (семантика не меняется); добавление `assistantMessage` аддитивно/обратносовместимо (поле уже опционально-nullable в схеме; новизна — оно теперь может быть НЕ-null при `tool_call`). Backend перестаёт отбрасывать сопутствующий текст (`orchestrator.py:661`) и кладёт его в `assistantMessage`.
  - `status=blocked` — `assistantMessage = null` (генерация не выполнялась).
  - **Согласование с историей и [ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md):** `assistantMessage` при `tool_call` = тот же текст, что отдают `text`-блоки `GET /v1/chats/{id}` → `steps[].payload.content[]` шага `stepId` (нормализация истории текстовые блоки не меняет — байт-в-байт хранилище). Инвариант: `ChatResponse.stepId` указывает на этот же assistant-шаг, поэтому run-проекция и история несут один и тот же сопутствующий текст.
- **`messageStepId` / `stepId` — идентификаторы синхронизации с историей чата ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md), nullable).** Позволяют клиенту склеить ответ генерации с шагами `GET /v1/chats/{id}` → `steps[]`. Обе величины уже существуют в orchestrator: `messageStepId` = `chat_steps.message_step_id` (ключ хода, см. §below про генерацию), `stepId` = `chat_steps.id` (PK конкретного шага). Семантика по статусам:
  - `status=assistant_message`: `messageStepId` = ход; `stepId` = `id` финального assistant-шага (= `ChatStepSchema.id` этого шага в истории). **Оба присутствуют.**
  - `status=tool_call`: `messageStepId` = ход; `stepId` = `id` assistant-шага, содержащего `tool_use` (тот шаг истории, чей `payload` несёт этот `tool_use`-блок). `toolCall.id` **остаётся как есть** (provider-независимый доменный id tool-вызова для `/chat/tool-result`) — `toolCall.id` ≠ `stepId`. **Оба присутствуют.**
  - `status=blocked` (**policy-blocked**, `blockReason ≠ max_tokens`): `messageStepId = null`, `stepId = null` — блокировка срабатывает в Policy Engine **до** генерации ([ADR-002](../../adr/ADR-002-access-policy-state-machine.md), [ADR-004](../../adr/ADR-004-blocked-http-200.md)), `chat_steps`/ход **не создаются**, ссылаться не на что (согласовано с отсутствием `usage` при policy-blocked).
  - `status=blocked` + **`blockReason=max_tokens`** (обрезка, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)): `messageStepId` = ход, `stepId` = `id` обрезанного assistant-шага — **оба НЕ null** (Claude сгенерировал контент, ход/шаг созданы). `usage` присутствует. Отличие от policy-blocked: здесь блокировка — обрыв **после** начала генерации, а не deny до неё.
- **Инвариант синка id шага/хода (нормативно):** `ChatResponse.messageStepId` / `ChatResponse.stepId` дословно совпадают с `ChatStepSchema.messageStepId` / `ChatStepSchema.id` соответствующего шага в [chats/02-api-contracts.md `GET /v1/chats/{id}` → `steps[]`](../chats/02-api-contracts.md#get-v1chatsid). Аддитивно/обратносовместимо: существующие поля, security, коды, пути не меняются ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)).
- **Инвариант синка имени/id инструмента (нормативно, [ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)):** `toolCall.name` (dot) и `toolCall.id` (domain UUID = `tool_calls.id`) этого ответа **дословно совпадают** с `tool_use.name`/`tool_use.id` соответствующего блока в `GET /v1/chats/{id}` → `steps[].payload.content[]` (история нормализует свой сырой wire-payload к доменному виду при отдаче — см. [chats/02-api-contracts.md](../chats/02-api-contracts.md#get-v1chatsid)) и с `name` в `/v1/tools`. Сопутствующий текст при `status=tool_call` (`text`-блок того же шага) в истории доступен полностью и **также** пробрасывается в `ChatResponse.assistantMessage` ([Q-024-1](../../99-open-questions.md) Closed = вариант A): тот же текст того же шага (`stepId`) — см. описание `assistantMessage` выше.

### Правила
- Перед генерацией — обязательный вызов Policy Engine (ADR-002).
- `status=blocked` → HTTP 200, машиночитаемый `blockReason` (ADR-004).
- Для `status=tool_call` payload строго типизирован по схемам ниже.
- Тех. ошибки (auth/size/validation/upstream) — 4xx/5xx (см. api-gateway).

## POST /v1/chat/tool-result
Приём результата(ов) локальных tools и продолжение шага. **Батч-форма ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))** — для parallel tool use возвращаются результаты на все `toolCalls[]` хода.

### Request (батч — рекомендуемая форма, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))
```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "results": [
    { "toolCallId": "uuid", "result": { "any": "object" } },
    { "toolCallId": "uuid", "error": { "code": "string", "message": "string" } }
  ]
}
```
- `results[]` — результаты на один или несколько tool-вызовов **одного хода**. В каждом элементе ровно одно из `result` / `error` (валидатор `extra=forbid` поэлементно).
- Каждый `result` ≤ 256KB (поэлементно).

### Request (одиночная форма — deprecated, обратная совместимость)
```json
{
  "userId": "uuid",
  "sessionId": "uuid",
  "toolCallId": "uuid",
  "result": { "any": "object" },
  "error": { "code": "string", "message": "string" }
}
```
- Эквивалентна `results = [{ toolCallId, result|error }]` (батч из одного). Backend принимает обе формы; одиночная — **deprecated** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)), удаление — отдельным ADR после миграции iOS.
- Ровно одно из `result` / `error`.
- `result` ≤ 256KB.

### Барьер хода и continuation ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md))
- Continuation-виток к Anthropic выполняется **ТОЛЬКО** когда для **всех** client-side `tool_use` текущего assistant-хода собраны `tool_result` (completed/errored). Иначе orphan `tool_use` → Anthropic `400` → `502`.
- **Рекомендуемый путь** — один батч-запрос со всеми результатами хода → барьер закрывается сразу, backend делает continuation и возвращает следующий шаг.
- **Накопительный путь (поддерживается):** результаты можно слать частями (несколько `/chat/tool-result` одного хода). Пока барьер не закрыт — ответ `status=tool_call` с `toolCalls[]` = **оставшиеся** (ещё без результата) client-side вызовы хода (`toolCall` = первый из оставшихся); Anthropic не вызывается; биллинг не выполняется. Когда последний результат закрывает барьер — continuation-виток, следующий шаг.
- Server-side `site.*` результаты в `/chat/tool-result` **не присылаются** — backend их сформировал сам ([ADR-011](../../adr/ADR-011-server-side-tools.md)); барьер хода учитывает только client-side tool-вызовы.

### Response (200)
Та же схема, что у `/v1/chat/run` (включая `messageStepId` / `stepId`, [ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md), `toolCalls[]`, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md), и `serverTools[]`, [ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md) — server-side, выполненные в **этом** continuation-витке).
- `messageStepId` **стабилен в рамках хода**: равен тому, что был выдан в исходном `/chat/run` этого хода (берётся из `tool_calls.message_step_id` по `toolCallId`, см. re-entry ниже) — это и есть смысл синка tool-loop: клиент держит один `messageStepId` на весь ход.
- `stepId` = `id` **нового** шага, который представляет этот ответ: assistant-tool_use следующего раунда (при `status=tool_call`) либо финальный assistant-шаг (при `status=assistant_message`). Ответ всегда указывает на **следующий шаг, порождённый Claude**, а не на только что принятый шаг-`tool_result`.
- `status=blocked` (если возникает на продолжении): `messageStepId`/`stepId` = `null` — как в `/chat/run`.

### Правила
- Проверка принадлежности каждого `toolCallId` текущей сессии: `tool_calls.session_id == sessionId`, иначе `404`/`403` (применяется к каждому элементу `results[]`).
- Re-entry message-шага: `messageStepId` берётся из `tool_calls.message_step_id` найденного `toolCallId` (НЕ генерируется заново). Все элементы батча должны относиться к одному ходу (один `message_step_id`). Все ответы и финальный debit этого шага используют тот же `messageStepId`.
- **Идемпотентность / повторы ([ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)):**
  - повторный `toolCallId` со статусом `completed`/`errored` → результат не перезаписывается, Anthropic повторно не вызывается; если барьер уже закрыт и continuation-шаг сохранён — вернуть его (как сейчас);
  - дубль `toolCallId` внутри одного батча → `422`;
  - continuation-виток к Anthropic выполняется **один раз** на закрытие барьера хода (дополнительно защищён `messageStepId`-идемпотентностью дебита, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- `result` валидируется по схеме соответствующего tool (см. ниже); несоответствие → `422`.

---

## Chat v2: режимный контракт `/v1/chat/v2/*`

Новый контракт генерации живёт **отдельно** от legacy `/v1/chat/*`: legacy остаётся прежним (полный локальный replay истории, фиксированная цена, без `generationMode`), режимные возможности доступны только здесь. Устройство слоёв (клиенты провайдеров, `provider_state`, repository) — [10-generation-modes-implementation.md](10-generation-modes-implementation.md); ниже — **wire-контракт**.

Три эндпоинта, все JWT-protected, как прочие `/v1/*`:

| Endpoint | Назначение |
|---|---|
| `POST /v1/chat/v2/run` | ход чата с выбором `generationMode` |
| `POST /v1/chat/v2/run/stream` | SSE text deltas + финальный `ChatResponse` ([ADR-069](../../adr/ADR-069-sse-text-streaming.md)) |
| `POST /v1/chat/v2/tool-result` | continuation tool-loop v2-хода (**без** `generationMode` в теле) |
| `GET /v1/chat/v2/capabilities` | список режимов и их цена для UI-переключателя |

### POST /v1/chat/v2/run

#### Request
Все поля [`POST /v1/chat/run`](#post-v1chatrun) (`userId`, `sessionId`, `projectId`, `message`, `mode`, `assistantMode`, `model`, `characterId`, `workspaceProjectId`, `attachments`, `context`, `editMessageStepId` — семантика, валидация и коды **идентичны**) **плюс одно**:

> **`attachments` здесь ровно тот же контракт**, включая приём на **любом** ходе сессии ([ADR-088](../../adr/ADR-088-attachments-per-turn-contract.md)), модерацию хода с вложениями ([ADR-086](../../adr/ADR-086-ugc-moderation.md), нарушение → `422 content_policy_violation`), разведённые коды ошибок и [лимиты](#лимиты-вложений-adr-089). Повышенный transport-лимит тела действует и на `/v1/chat/v2/run`, и на `/v1/chat/v2/run/stream` ([ADR-089 §1](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md)).

```json
{ "generationMode": "general | research | reasoning | study_learn" }
```
<a id="generationmode-adr-064"></a>
- **`generationMode` (опц., дефолт `general`, per-turn).** Не фиксируется на сессию: в одном `sessionId` ход может быть `research`, следующий — `general`, затем `study_learn`. Значение вне набора → `422` (`StrictModel`/`Literal`). Отдельной оси «режим диалога» (`dialogMode`) в контракте **нет** — режим один ([ADR-064 §1](../../adr/ADR-064-study-learn-quiz-generation-mode.md)).
- **Контраст с соседним полем `characterId`: правила противоположны, не переносить по аналогии.** `generationMode` — **per-turn**, приходит в каждом запросе, персистится на user-шаге и может меняться внутри одной сессии. `characterId` ([§выше](#characterid-опц-session-fixed-adr-097)) — **session-fixed**: принимается только при создании сессии, на resume игнорируется, внутри сессии не меняется ([ADR-097 §4](../../adr/ADR-097-character-personas.md): в реплеенной истории остаются реплики прежнего персонажа, и смена голоса посреди чата даёт смешанного собеседника — в отличие от режима, который меняет лишь то, что ассистент делает на этом ходе).
- Значение персистится в user-шаге хода (`chat_steps.payload.generationMode`) — из него continuation восстанавливает режим (см. `/v1/chat/v2/tool-result`).
- **Что даёт режим:**

  | Режим | Провайдерские возможности | Инструменты сверх обычного набора | Цена (дефолт) |
  |---|---|---|---|
  | `general` | обычная генерация | — | `CHAT_CREDIT_COST_GENERAL` = 1 |
  | `research` | hosted web search (оба провайдера) | — | `CHAT_CREDIT_COST_RESEARCH` = 3 |
  | `reasoning` | reasoning effort (OpenAI) / extended thinking (Anthropic) | — | `CHAT_CREDIT_COST_REASONING` = 3 |
  | `study_learn` | **никаких** (по knobs = `general`) | **`quiz.generate`** ([§ниже](#quizgenerate--server-side-global-tool-режимный-adr-064)) | `CHAT_CREDIT_COST_STUDY_LEARN` = 2 |

- **Биллинг:** цена режима берётся **единственным** мостом `chat_generation_credit_cost(mode)` и используется и для проверки баланса до генерации, и для финального идемпотентного дебита по `messageStepId` ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Режим не может быть допущен по одной цене и списан по другой. BYOK/trial внутренние кредиты не тратят (как раньше).
- **Сессия и backend contract:** сессия помечается `generation_backend='v2'`; legacy-роут не может продолжить v2-сессию, а `/v1/chat/v2/run` может явно апгрейдить старую/`NULL`-сессию в v2 ([10-generation-modes-implementation.md](10-generation-modes-implementation.md#_ensure_session_backend)).

#### Response (200)
Та же схема `ChatResponse`, что у [`POST /v1/chat/run`](#response-200) — те же `status`/`sessionId`/`messageStepId`/`stepId`/`assistantMessage`/`toolCalls[]`/`toolCall`/`blockReason`/`serverTools[]`/`usage` — **плюс два отличия**:

1. `usage` дополнительно несёт `generationMode` и (при фактическом дебите) `creditsCharged`.
2. <a id="quiz-adr-064"></a>**`quiz` (аддитивно, nullable, **turn-scoped**, [ADR-064 §7](../../adr/ADR-064-study-learn-quiz-generation-mode.md))** — структура квиза **хода** (`messageStepId`), а не текущего вызова:

```json
{
  "quiz": {
    "questions": [
      {
        "question": "Что делает оператор `await` в Swift?",
        "options": ["Блокирует поток", "Приостанавливает задачу до готовности результата", "Создаёт новый поток"],
        "correctIndex": 1,
        "explanation": "`await` приостанавливает текущую задачу, не блокируя поток."
      }
    ]
  }
}
```

- Поле присутствует **всегда**; `null` = «квиза не было **в этом ходе** (`messageStepId`)», **не** «не было в этом вызове» (см. turn-scope ниже) — аддитивно и обратносовместимо: клиенты, не знающие о поле, игнорируют.
- **Семантика — TURN-scoped (нормативно, НЕ «за один вызов»).** `quiz` любого ответа = **последний валидный пул этого ХОДА** (`messageStepId`). Одинаково на **всех** ногах хода: `/v1/chat/v2/run`, каждый `/v1/chat/v2/tool-result`-continuation, идемпотентный реплей закрытого хода, `blocked`+`max_tokens`.
  - **Producer 1 — аккумулятор текущего вызова.** Несколько валидных вызовов инструмента в одном обращении → **last-wins**; пулы не склеиваются.
  - **Producer 2 — фолбэк.** Аккумулятор этого вызова пуст **И** эффективный режим хода = `study_learn` → взять последний tool-шаг хода с `toolName = quiz.generate` и непустым `result`. Нет такого шага → `null`. Предикат режима обязателен: вне квиз-ходов (все прочие режимы и весь legacy) дополнительной выборки не делается.
- **Зачем turn-scope (несущая конструкция анти-спойлерной гарантии).** Подавление `assistantMessage` (ниже) ключевано на непустом `quiz`. Штатный ход, где модель в одном assistant-шаге вызвала `quiz.generate` **и** client-side инструмент, состоит из двух ног: `run` → `tool_call` (+пул) и `tool-result` → финальный `assistant_message`. При семантике «за один вызов» вторая нога отдала бы `quiz=null`, подавление не сработало бы и пользователь получил бы дубль вопросов с раскрытыми ответами. То же — на сетевом ретрае закрытого хода. Turn-scoped-правило закрывает обе ноги и ретрай одним предикатом.
- **Клиент трактует `quiz` как содержимое хода, а не дельту:** один и тот же пул может прийти в нескольких ответах одного `messageStepId` — карточки **заменяются** (идемпотентно), не накапливаются.
  > **Контраст с `serverTools[]` — намеренно противоположно.** `serverTools[]` **per-call** и при реплее **пуст** ([ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)): это индикатор «что выполнилось в ЭТОМ вызове», реконструкция была бы ложью. `quiz` — **контент хода**, и его пропажа на любой ноге немедленно снимает подавление текста (спойлер вместо карточек). Не переносить правило одного поля на другое ни в одну сторону.
- **`assistantMessage = null` при непустом `quiz` (нормативно).** Это **исключение** из описания `assistantMessage` выше: текст не отдаётся, когда в ответе есть `quiz`, — **при любом статусе**, где `quiz` непуст (`assistant_message`, `tool_call`, `blocked`+`max_tokens`; в последнем случае подавляется и частичный текст обрыва, прочие правила `max_tokens` — `usage`/`messageStepId`/`stepId` присутствуют, кредит не списан — не меняются). Причина — детерминированная защита от дубля вопросов и спойлера правильных ответов в свободном тексте; правило применяется в единственной точке маппинга ответа и ключевано **на присутствии `quiz`**, поэтому не может сработать на legacy-ходе. Частично переопределяет [ADR-024 п.3](../../adr/ADR-024-history-payload-domain-normalization.md) — **только** для ходов с квизом. **Хранение** и **реплей провайдеру** при этом не меняются: сырой assistant-шаг (с текстом, если он был) сохраняется и реплеится как есть. **Отдача истории — меняется ([ADR-065 §2](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md), пересматривает [ADR-064 §7](../../adr/ADR-064-study-learn-quiz-generation-mode.md)):** у ходов с непустым квизом текстовые блоки assistant-шагов **срезаются** при отдаче `GET /v1/chats/{id}`/`/steps`/превью — иначе холодный старт приложения посреди квиза показывал бы спойлер. Правило и контраст с [ADR-042](../../adr/ADR-042-hide-context-block-from-user-facing-history.md) — [chats/02-api-contracts.md §квиз-ход](../chats/02-api-contracts.md#quiz-strip-adr-065).
- Проверка ответов пользователя — **на клиенте**; эндпоинта отправки/проверки ответов нет и не вводится ([ADR-064 §8](../../adr/ADR-064-study-learn-quiz-generation-mode.md)).
- Все прочие правила ответа (blocked=200, `max_tokens`, sync-id, барьер хода) — без изменений.

### POST /v1/chat/v2/run/stream — SSE text streaming ([ADR-069](../../adr/ADR-069-sse-text-streaming.md))

Тот же body/auth/rate-limit **и тот же transport-лимит тела**, что у [`POST /v1/chat/v2/run`](#post-v1chatv2run) ([ADR-089 §1](../../adr/ADR-089-attachment-limits-and-error-taxonomy.md) — роут принимает те же `attachments[]`, поэтому подпадает под `ATTACHMENT_REQUEST_BODY_LIMIT`, а не под общий 512 KB). Ответ: `Content-Type: text/event-stream`.

Отказы, вынесенные **до** старта стрима (валидация вложений, модерация, policy-ошибки), приходят обычным HTTP-ответом `4xx`/`5xx` в стандартном формате ошибки — SSE-кадр `error` используется только для сбоя **после** старта стрима.

| event | data | когда |
|-------|------|--------|
| `delta` | `{ "text": "<incremental>" }` | кусок текста ассистента (не в `study_learn`) |
| `done` | полный `ChatResponse` | конец хода |
| `error` | `{ "code", "message" }` | сбой после старта стрима |

Наращивать UI по `delta`; истина — `done.assistantMessage`. В `study_learn` дельт нет. JSON `/v2/run` без изменений; `/v2/tool-result` без stream в этой итерации.

**Бриф для iOS (код не в этом репо):** новый URL `/v1/chat/v2/run/stream`; парсить SSE (`event` + JSON `data`); UI растёт по `delta.text` вместо индикатора «думает»; на `done` применить полный `ChatResponse` (`toolCalls` / `mediaJobs` / `mediaChoices` / `quiz` как у JSON `/v2/run`); keep-alive / reconnect в v1 не обязателен (один запрос = один ход).

**Бриф mediaChoices (ADR-070):** при непустом `mediaChoices` — карточки как квиз (`question` + tap по `options[].label` или `value`; цена: `options[].credits` и/или хвост label `· N cr.`; на resolution/duration/audio цены также в тексте `question`). Поле `prompt` **нет** — не показывать fal-промпт. Шаг `useLastImage` (video после сгенерированного фото): вопрос «Использовать последнее фото?», options `true`/`Да` и `false`/`Нет` — тот же UI, что у model/duration. Накопить `answers[id]=value` и слать `mediaSelection` на `/v2/run` (пустой `message` ок); повторять до `mediaJobs`. Промежуточные тапы **не** плодят шаги в истории — на финале один user `Media: <kind> · <model> · … · N cr.` (без текста промпта) + assistant с `payload.mediaJobs`. **Cold start / история:** в `GET /v1/chats/{id}` искать `steps[].payload.mediaJobs` на последнем assistant хода (`jobId` → poll/push media); то же для пути `media.generate_*`. Правки («дорисуй…») — модель шлёт `sourceJobId`; ассеты из `GET /v1/media/jobs/{jobId}`. Каталог `GET /v1/media/models` для отдельного media-UI валиден.

### POST /v1/chat/v2/tool-result

Request/Response — **идентичны** [`POST /v1/chat/tool-result`](#post-v1chattool-result) (батч `results[]`, deprecated одиночная форма, барьер хода, идемпотентность), с одним нормативным отличием:

- **`generationMode` в теле НЕ принимается** (лишнее поле → `422`). Режим хода восстанавливается из user-шага исходного `/v1/chat/v2/run` (`chat_steps.payload.generationMode`). Восстановленный режим определяет **и цену continuation-а, и tool-набор** очередного витка — включая `quiz.generate` для `study_learn`. Допустимый набор восстановления — все четыре режима; неизвестное значение деградирует к `general`.
- `/v1/chat/v2/tool-result` **не** апгрейдит legacy-сессию в v2 (continuation уже начатого хода), а legacy `/v1/chat/tool-result` не может продолжить v2-ход.

### GET /v1/chat/v2/capabilities

Backend-level объявление режимов для UI-переключателя. Пользовательские баланс/подписка здесь **не** проверяются — это решает конкретный `/v1/chat/v2/run` (blocked=200, [ADR-004](../../adr/ADR-004-blocked-http-200.md)).

#### Auth
- **JWT-protected** (как `GET /v1/tools`/`GET /v1/models`). Метод `GET` (read-only, кэшируемо), per-user rate-limit как у прочих read-эндпоинтов.

#### Response (200)
```json
{
  "provider": "openai",
  "defaultGenerationMode": "general",
  "generationModes": [
    {"mode": "general", "creditCost": 1, "available": true},
    {"mode": "research", "creditCost": 3, "available": true},
    {"mode": "reasoning", "creditCost": 3, "available": true}
  ],
  "reasoningLevel": "medium"
}
```
- `provider` — активный LLM-провайдер инстанса (`LLM_PROVIDER`, нормализован).
- `defaultGenerationMode` — режим при отсутствии поля в запросе (`general`).
<a id="generationmodes--гейт-объявления-adr-065"></a>
- **`generationModes[]` — режимы, которые этот инстанс ОБЪЯВЛЯЕТ (не «все, которые backend понимает», [ADR-065 §1](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)).** Состав задаётся env-allowlist **`CHAT_ADVERTISED_GENERATION_MODES`** (образец — allowlist моделей `ANTHROPIC_MODELS`/`OPENAI_MODELS`, [ADR-034](../../adr/ADR-034-user-model-selection.md): env управляет тем, что показано в каталоге, а не тем, что умеет backend):
  - **режим вне allowlist ОТСУТСТВУЕТ в массиве** (не помечается `available:false`);
  - **дефолт** (env не задан / пуст / целиком невалиден) — `general,research,reasoning`; **`study_learn` по умолчанию НЕ объявляется** (fail-closed: цена ошибки «не показали» — нет фичи, цена обратной — списанные кредиты и пустой экран у приложения без квиз-UI);
  - **`general` присутствует всегда**, даже если не перечислен в env (`defaultGenerationMode` обязан быть в списке);
  - **неизвестные значения игнорируются + WARNING** (graceful-разбор, не startup-crash);
  - `creditCost` — **см. врезку ниже: с [ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md) цена зависит от МОДЕЛИ, а не от режима.**

<a id="creditcost--цена-по-модели-adr-099"></a>
> ⚠️ **`creditCost` после [ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md): цена хода — функция МОДЕЛИ, режим на неё не влияет.**
> - **Источник — тот же единственный мост «цена–гейт–списание»** ([ADR-064 §9](../../adr/ADR-064-study-learn-quiz-generation-mode.md)), переключённый с ключа «режим» на ключ «модель»: одна функция питает pre-generation balance-гейт, финальное списание и это поле. Второго механизма цены по-прежнему не существует.
> - **Без параметра `?model=` поле несёт ПОТОЛОК** — максимум цены по **полному известному каталогу моделей инстанса** (`allowed_models_union()`), одинаковый у всех элементов массива. ⛔ **Не по витрине (`chat.models_offered`):** модель, снятая с витрины, продолжает обслуживать созданные сессии и списывать по своей цене ([ADR-099 §4.3](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md)), поэтому максимум по витрине занижал бы объявленную цену относительно фактического списания. Правило: **агрегат никогда не занижает фактическое списание** (показать меньше, чем спишется, — скрытая переплата; показать больше — видимая и безопасная сторона ошибки).
> - **`?model=<id>` (аддитивный, опциональный)** — `creditCost` каждого элемента равен точной цене этой модели; неизвестный `model` → `422`. Точная цена также в `creditCost` chat-строк [`GET /v1/models`](../../API-REFERENCE.md#get-v1models).
> - Значение правится оператором из CRM без деплоя и применяется в течение окна, объявленного admin-контрактом, — клиенту его **нельзя кэшировать надолго**.
- **Гейт ОБЪЯВЛЕНИЯ ≠ гейт ПОВЕДЕНИЯ (нормативно).** [`POST /v1/chat/v2/run`](#post-v1chatv2run) принимает `generationMode=study_learn` **на любом инстансе**, независимо от allowlist: приложение, знающее имя режима, работает. Allowlist влияет **только** на состав этого массива. Per-instance флаг **включения** режима (запрос отвергается) — отклонён и не вводится; цена выключателем служить не может (кламп `≤0 → 1`, [§Config](10-generation-modes-implementation.md#config)).
- **`available`** — у **присутствующих** элементов всегда `true`; producer'а, возвращающего `false`, **нет**. Клиент обязан читать гейт как **присутствие/отсутствие элемента**, а не как значение `available` ([ADR-065 §1.8](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)). Поле сохранено для совместимости и зарезервировано под будущее «объявлен, но недоступен» — это будет отдельное решение.
- **Порядок фиксирован и каноничен:** `general`, `research`, `reasoning`, `study_learn` — независимо от порядка перечисления в env; новые режимы добавляются **в конец**, позиции существующих не сдвигаются (клиент вправе рендерить список как есть).
- `reasoningLevel` — серверный effort/budget level для `reasoning` (`low|medium|high`, `CHAT_REASONING_LEVEL`).
- **Forward-compat:** клиент обязан игнорировать неизвестные ему значения `mode` в списке — появление нового режима не является breaking change.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.

---

## Классы tools: client-side vs server-side ([ADR-011](../../adr/ADR-011-server-side-tools.md), [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md))
Три класса инструментов ([ADR-026 §1](../../adr/ADR-026-global-server-side-tools-and-time-now.md)):
- **client-side** (`files.*`, `calendar.*`, `reminders.*`, `git.*` [ADR-094](../../adr/ADR-094-code-assistant-tools.md), `maps.*` [ADR-102](../../adr/ADR-102-mapkit-client-tools.md)) — исполняет **iOS-клиент**: backend отдаёт `status=tool_call`,
  ждёт `tool_result` через `/v1/chat/tool-result`. Описаны в этом документе.
- **server-side, project-scoped** (`site.*`, website-builder, `SERVER_SIDE_TOOLS`) — исполняет **backend** немедленно в tool-loop, формирует `tool_result` сам
  и продолжает к Anthropic **без** round-trip к iOS; **НЕ** отдаётся клиенту как `status=tool_call`. **Требует проекта.** Схемы и поведение —
  [modules/website-builder/02-api-contracts.md](../website-builder/02-api-contracts.md), [ADR-011](../../adr/ADR-011-server-side-tools.md).
- **server-side, global** (`time.now`, `quiz.generate`, `document.*` — [ADR-090](../../adr/ADR-090-chat-documents.md), `GLOBAL_SERVER_SIDE_TOOLS`, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)/[ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) — исполняет **backend** немедленно в tool-loop (как `site.*`), но **НЕ требует проекта**. В `toolCalls[]` наружу **НЕ** попадают. Контракты — [§`time.now`](#timenow--server-side-global-tool-adr-026) и [§`quiz.generate`](#quizgenerate--server-side-global-tool-режимный-adr-064) ниже.
  - **Предложение модели внутри класса различается (не переносить по аналогии!):** `time.now` предлагается **ВСЕГДА** (utility, [ADR-026 §3](../../adr/ADR-026-global-server-side-tools-and-time-now.md)); `quiz.generate` — **только** когда эффективный режим хода = `study_learn` (ось C, [ADR-064 §3](../../adr/ADR-064-study-learn-quiz-generation-mode.md)). «Global» означает «без проекта», а не «без гейта».
> **`document.*` — НЕ `files.*`.** Одноимённые по смыслу семейства исполняются в разных местах: `files.read`/`files.write` исполняет **устройство пользователя** (client-side, как `calendar`/`reminders`), `document.*` живут на **бэкенде**, переживают ход и скачиваются клиентом по REST ([ADR-090](../../adr/ADR-090-chat-documents.md)). Правило одного семейства на другое не переносится.

- Orchestrator различает класс по доменному имени (статические реестры `SERVER_SIDE_TOOLS = {site.*}`, `GLOBAL_SERVER_SIDE_TOOLS = {time.now, quiz.generate, media.generate_image, media.generate_video, media.ask_params, document.create, document.list, document.read, document.update}`, непересекающиеся). Дополнительный реестр `TOOL_GENERATION_MODES` (`quiz.generate → {study_learn}`) задаёт ось C; инструменты вне этого реестра по режиму не гейтятся. domain↔anthropic
  mapping (точка→подчёркивание) расширяется server-side именами (`site.write_file ↔ site_write_file`, `time.now ↔ time_now`, …). Guard на число
  server-side раундов — `MAX_SERVER_TOOL_ROUNDS` (дефолт 16) — общий для project-scoped и global server-side раундов.
- **Гейтинг по наличию проекта ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)):** `site.*` (`SERVER_SIDE_TOOLS`) предлагаются Claude **только** когда у сессии есть `project_id` (создана с `projectId`). В «чистом чате» (`chat_sessions.project_id IS NULL`) `site.*` в tool-набор **не включаются** — Claude их не видит и не вызывает. **`time.now` (`GLOBAL_SERVER_SIDE_TOOLS`) под этот гейт НЕ подпадает** — предлагается всегда ([ADR-026 §3](../../adr/ADR-026-global-server-side-tools-and-time-now.md)). См. [03-architecture.md §Гейтинг tools](03-architecture.md#гейтинг-site-tools-по-наличию-проекта-adr-022).
- **Гейтинг по режиму генерации (ось C, [ADR-064 §3](../../adr/ADR-064-study-learn-quiz-generation-mode.md)):** инструмент из `TOOL_GENERATION_MODES` предлагается **только** в перечисленных режимах. Гейт считается по **эффективному** режиму хода — тому же значению, которое уходит провайдеру и в биллинг. Legacy по умолчанию `general`; при `CHAT_LEGACY_WEB_SEARCH_ENABLED` — `research` ([ADR-082](../../adr/ADR-082-legacy-web-search.md)). `quiz.generate` только `study_learn`, поэтому на `/v1/chat/run` не предлагается. Оси A/B/C/D/E складываются по И — таблица «инструмент × оси» в [03-architecture.md §Оси гейтинга tool-набора](03-architecture.md#оси-гейтинга-tool-набора-adr-022--adr-026--adr-064).
- **Гейтинг инструментов кода (ось D, [ADR-094 §3](../../adr/ADR-094-code-assistant-tools.md)):** инструменты из `CODE_TOOLS` (`files.search`/`patch`/`delete`/`move`, `git.*`) предлагаются модели, только когда флаг инстанса `CODE_TOOLS_ENABLED=true` **и** сессия в режиме `assistant_mode=code`. Дефолт — **выключено**. Причина не в осторожности, а в контракте tool-loop: инструмент, который модель позвала, а клиент исполнить не умеет, оставляет ход незавершённым — бэкенд ждёт `tool-result`, которого не будет. Поэтому флаг снимается инстанс за инстансом по мере готовности клиента ([Q-094-2](../../99-open-questions.md)). На каталог `GET /v1/tools` ось D, как и A/B/C/E, **не** влияет.
- **Гейтинг инструментов карт (ось E, [ADR-102 §10](../../adr/ADR-102-mapkit-client-tools.md)):** `maps.*` предлагаются модели, только когда флаг инстанса `MAPS_TOOLS_ENABLED=true`. Дефолт — **выключено**, причина та же, что у оси D: позванный, но неисполнимый инструмент оставляет ход незавершённым (барьер [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) ждёт `tool-result`, которого не будет; ни таймаута, ни сборщика «протухших» вызовов в коде нет). Раскатка — инстанс за инстансом по готовности приложения ([Q-102-1](../../99-open-questions.md)); частичная реализация семейства недопустима. **Отличие от оси D:** с `assistant_mode` ось E **не** складывается — вопрос «как доехать» это обычный чат, а не режим. **Guard (в отличие от оси D — есть):** `maps.*` при выключенном флаге модели не предлагаются, а если она всё же вернёт такое `tool_use` — бэкенд клиентский вызов **не создаёт**, а мягко отказывает (`tool_not_available`, `tool_calls` → `errored`, ход продолжается), тем же механизмом, что денилист [ADR-081](../../adr/ADR-081-disabled-tool-families.md). Мягко отклонённый вызов попадает в `serverTools[]` записью со `status="errored"` — так уже ведут себя отказы `files.*` по денилисту: там отражается **действие бэкенда** (отказ), а не исполнение клиентского инструмента. У оси D такого guard'а **нет** ([TD-044](../../100-known-tech-debt.md)) — не переносить отсутствие guard'а с одной оси на другую. На каталог `GET /v1/tools` ось E, как и A/B/C/D, **не** влияет.

## `time.now` — server-side global tool ([ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md))
Инструмент текущей даты/времени. Исполняет **backend** в tool-loop (без round-trip к iOS, как `site.*`), но **БЕЗ проекта** — доступен в любом ходе, включая основной flow чат-агрегатора ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)). Решает репорт «модель отвечает 2024 год»: системный промт статичен и не несёт даты, модель получает время только из результата `time.now`. Не мутирующий (нет `tool_mutation` audit). В `toolCalls[]` наружу не отдаётся (исполнен на бэке).

### Args (`TimeNowArgs`, Pydantic v2, `extra="forbid"`)
```json
{ "tz": "Europe/Moscow" }
```
- `tz` (опц., `str | None`, default `null`) — IANA-имя зоны (напр. `Europe/Moscow`, `America/New_York`). Лимит длины `≤ 64` символа ([Q-026-1](../../99-open-questions.md)). При отсутствии → результат только в UTC.
- `extra="forbid"`: любой иной ключ → ошибка валидации args.

### Result
```json
{
  "utc": "2026-06-10T14:23:05.123456+00:00",
  "unix": 1781446985,
  "weekday": "Wednesday",
  "timezone": "Europe/Moscow",
  "local": "2026-06-10T17:23:05.123456+03:00"
}
```
- `utc` — **всегда**: текущее UTC, ISO8601 (RFC3339) с offset `+00:00`.
- `unix` — **всегда**: целочисленный Unix timestamp (секунды, UTC).
- `weekday` — **всегда**: английское имя дня недели по UTC-дате (`Monday`..`Sunday`).
- `timezone` — **только** при заданном валидном `tz`: нормализованное IANA-имя.
- `local` — **только** при заданном валидном `tz`: ISO8601 с локальным offset.
- Без `tz` → `timezone`/`local` **опущены** (только UTC-набор).

### Ошибки и инварианты
- **Невалидный/неизвестный `tz`** (не парсится `zoneinfo` / `ZoneInfoNotFoundError` / длина > 64) → **tool-result error** `{"error":{"code":"invalid_timezone","message":"..."}}` (через `ToolExecution.error`), **НЕ** падение хода (не `422`, не `502`). Claude получает машиночитаемую ошибку и может повторить без `tz`/с корректной зоной; ход продолжается.
- **UTC-набор от tz-базы не зависит** (вычисляется от `datetime.UTC`) и доступен всегда. Локальное время по `tz` требует tz-базы в образе — обеспечена pure-Python зависимостью `tzdata` ([TD-019](../../100-known-tech-debt.md) **Resolved 2026-06-10**, вариант A); `tz` в prod работает. Невалидная/мусорная зона по-прежнему деградирует к tool-result error `invalid_timezone` (резолв ловит `ZoneInfoNotFoundError`/`ValueError`/`OSError`).
- **Биллинг:** раунд `time.now` не добавляет списаний — 1 кредит = 1 сообщение ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)); списание один раз на финальном `assistant_message`.
- **Clock-провайдер:** время берётся через инъектируемый `Clock` (детерминизм qa, [ADR-026 §8](../../adr/ADR-026-global-server-side-tools-and-time-now.md), [06-testing-strategy.md](../../06-testing-strategy.md)), не прямой `datetime.now()`.

<a id="quizgenerate--server-side-global-tool-режимный-adr-064"></a>
## `quiz.generate` — server-side global tool, режимный ([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md))

Инструмент выдачи **пула вопросов** обучающего квиза. Исполняет **backend** в tool-loop (без round-trip к iOS, как `time.now`), проекта не требует, **предлагается модели только при эффективном `generationMode = study_learn`** (ось C). Не мутирующий (нет `tool_mutation` audit), отдельных списаний не вводит. В `toolCalls[]` наружу не отдаётся; результат поднимается в [`ChatResponse.quiz`](#quiz-adr-064).

«Исполнение» = **валидация аргументов + эхо-возврат** того же объекта как tool-result.

### Args (`QuizGenerateArgs`, Pydantic v2, `extra="forbid"` на обёртке и на каждом вопросе)
```json
{
  "questions": [
    {
      "question": "Что делает `await` в Swift?",
      "options": ["Блокирует поток", "Приостанавливает задачу", "Создаёт поток"],
      "correctIndex": 1,
      "explanation": "`await` приостанавливает задачу, не блокируя поток."
    }
  ]
}
```

| Поле | Тип | Ограничение (нормативно) |
|---|---|---|
| `questions` | array\<object\> | **3..10** элементов (нижняя граница = осмысленный пул, верхняя = потолок токенов/латентности) |
| `questions[].question` | string | непустая, ≤ **1000** символов |
| `questions[].options` | array\<string\> | **2..10** вариантов, каждый непустой, ≤ **400** символов |
| `questions[].correctIndex` | integer | 0-based, `0 ≤ correctIndex < len(options)`; **`bool` не принимается** (в Python `bool` — подтип `int`, проверять явно) |
| `questions[].explanation` | string | непустая, ≤ **2000** символов |

- Все поля **обязательны**; любой иной ключ → ошибка валидации args.
- **JSON Schema инструмента (`inputSchema` в `GET /v1/tools` и `input_schema`/`parameters`, уходящие провайдеру) обязана быть self-contained — без `$ref`/`$defs`:** вложенная модель вопроса инлайнится. Поддержка `$ref` у двух разных провайдеров не гарантирована, и опираться на неё контракт не должен.
- **Ограничивающие ключи (`minItems`/`maxItems`/`maxLength`) в схеме — ОБЯЗАТЕЛЬНЫ, а не «остаются» ([ADR-065 §4](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)).** Strict-режим у tools в этой интеграции выключен, провайдер эти ключи не отвергает, поэтому **каждое** числовое ограничение пула, выразимое в JSON Schema, обязано быть в ней выражено — включая `options.items.maxLength` (лимит длины **варианта ответа**). Реализация ограничения кастомным валидатором **вместо** ключа схемы — дефект: модель узнаёт о нарушении только из degrade-раунда, а это лишний upstream-вызов на ходу ценой 2 кредита. Серверная проверка остаётся авторитетной и не отменяется — ключи схемы это **подсказка**, а не гарантия.
- **Структура вопроса объявляется ОДИН раз ([ADR-065 §5](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)).** Модель аргументов инструмента и wire-модель поля `quiz` ответа обязаны иметь **общий источник** (вторая переиспользует/строится из первой). Если реализация держит два объявления — обязателен **механический тест паритета** (имена полей, типы, обязательность, границы): иначе расхождение проявится не на сборке, а как ошибка валидации на живом ходе у пользователя.

### Result (эхо)
```json
{ "questions": [ { "question": "…", "options": ["…", "…"], "correctIndex": 1, "explanation": "…" } ] }
```
Тот же объект, что пришёл в args, после успешной валидации. Он же: (а) сохраняется как обычный tool-результат в `chat_steps.payload` tool-шага (`toolName = quiz.generate`, поле `result`); (б) поднимается в `ChatResponse.quiz` вызова.

### Ошибки и инварианты
- **Любое** нарушение ограничений выше → **tool-result error** `{"error":{"code":"invalid_quiz","message":"…"}}`, ход **НЕ падает** (не `422`, не `502`). Модель видит ошибку в том же ходе и перегенерирует пул. Это **исключение** из общего правила «невалидные args инструмента → `422`»: `quiz.generate` входит в реестр `ARGS_DEGRADE_TOOLS`. Обоснование — провайдерского strict-режима нет, межполевые инварианты (`correctIndex < len(options)`, число вопросов/вариантов, длины) не гарантирует никто, кроме нас, поэтому нарушение — **ожидаемый**, а не аномальный сценарий ([ADR-064 §5](../../adr/ADR-064-study-learn-quiz-generation-mode.md)). Прецедент в этом же коде — `invalid_timezone` у [`time.now`](#timenow--server-side-global-tool-adr-026).
  > **Контраст (не переносить по аналогии):** для **всех остальных** инструментов невалидные args по-прежнему дают `ValidationFailedError` → **`422`** на ход. Их схемы фиксированы контрактом, и кривой args там — настоящая аномалия. Ветки соседние, поведение противоположно — намеренно.
- **All-or-nothing:** невалидный **любой** вопрос делает невалидным **весь** пул (вопросов вне `3..10`, пустой список, вариантов вне `2..10`, over-length поле, `correctIndex` булев/отрицательный/вне диапазона) → один `invalid_quiz`, модель перегенерирует весь пул. Частичное принятие (выкинуть плохой вопрос) — запрещено.
<a id="degrade-message--нормативные-границы"></a>
- **`message` ошибки — content-free И ограничен по размеру (нормативно).** Строится из пути поля (`loc`) и типа ошибки валидации (например `questions.2.correctIndex: out of range; expected 3-10 questions, 2-10 options, 0-based correctIndex < len(options)`), **без значений** полей — текст квиза в сообщение не попадает. Сверх этого действуют **три обязательных предела**, все — жёсткие срезы, а не семантические ограничения:

  | Измерение | Значение | Зачем именно так |
  |---|---|---|
  | число записей об ошибках в сообщении | **5** | ошибок в пуле может быть десятки (по одной на каждый вопрос); модели для исправления достаточно первых |
  | длина одной записи | **120** символов | переиспользуется **существующий** лимит компактной строки `serverTools[].summary` — второе число для той же задачи «короткая машинно-адресованная строка» не вводится |
  | длина склейки (итогового `message`) | **400** символов | **не** `5 × 120`: сообщение обязано остаться пригодной **инструкцией для модели**, а вызывающая сторона дописывает к нему подсказку про ограничения пула; персистируемый результат должен остаться читаемым |

  - **Нужны ВСЕ ТРИ предела, а не один.** Ограничение только числа записей оставляет размер зависимым от ввода: при лишнем ключе (`extra_forbidden`) в `loc` попадает **имя ключа, которое придумала модель**, то есть произвольная строка. Поэтому режется и каждая часть, и склейка. Это тот же защитный паттерн, что жёсткий cap на `serverTools[].summary`.
  - **Почему это нормативное требование, а не деталь реализации.** Сообщение **персистится** в `chat_steps` tool-шага и **реплеится модели** на следующем витке — то есть его размер входит и в объём БД, и в каждый последующий промпт хода. Размер артефакта, который порождает модель и потребляет она же, не может зависеть от того, что она прислала.
  - Пределы применяются и к не-pydantic ошибкам валидации args (там сообщение уже content-free по построению, но срез по длине склейки действует).
- **Вызов вне режима** (модель вернула `quiz.generate` там, где он не предлагался — upstream-аномалия): backend инструмент **не исполняет** → tool-result error `{"error":{"code":"tool_not_available", …}}`, tool_call → `errored`, ход продолжается, `quiz` в ответе остаётся `null`.
- **Приоритет двух отказов (нормативно):** проверка режима выполняется **раньше** валидации аргументов. При пересечении (инструмент вызван вне режима **и** с невалидным пулом) отдаётся **`tool_not_available`**, а не `invalid_quiz`. Иначе модель получила бы задание чинить пул и продолжила бы упираться в недоступный инструмент, сжигая server-side раунды до `MAX_SERVER_TOOL_ROUNDS`. Порядок фиксируется явно, а не следует из расположения веток в коде.
  > **Контраст с `site.*` ([ADR-022 §guard](../../adr/ADR-022-optional-project-and-tool-gating.md)):** `site.*` в сессии без проекта → **жёсткий** отказ хода (`UpstreamError` → `502`), потому что исполнение потребовало бы резолва проекта — это граница изоляции данных (IDOR). У `quiz.generate` побочных эффектов нет вообще, поэтому отказ **мягкий**. Поведение этих двух guard'ов различается намеренно.
- **Граница повторов:** каждая неудачная попытка расходует server-side раунд tool-loop'а; упорство модели упирается в общий `MAX_SERVER_TOOL_ROUNDS` (дефолт 16) → audit `max_server_tool_rounds_exceeded`, `502`, **без биллинга** ([ADR-011 §2](../../adr/ADR-011-server-side-tools.md)). Квиз-специфичной «мягкой посадки» нет.
- **`serverTools[]`:** выполнение отражается обычной записью ([ADR-028](../../adr/ADR-028-projectid-in-chat-list-and-server-tools-in-chat-response.md)/[ADR-030](../../adr/ADR-030-toolcallid-in-server-tools.md)) — `toolName = "quiz.generate"`, `status = completed|errored`, `summary` = `"ok"` либо код ошибки (`invalid_quiz`/`tool_not_available`). **Содержимое квиза в `summary` не попадает.**
- **Биллинг:** раунд `quiz.generate` списаний не добавляет; списание — один раз на финальном `assistant_message` по цене режима `study_learn` ([ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)).
- **Приватность:** `correctIndex` намеренно уходит клиенту (обучающий сценарий, не экзамен). Audit хранит только `toolCallId`/`toolName`/`status` — текст квиза туда не пишется.

## Tools (backend ↔ iOS, client-side) — строго типизированные схемы
Backend только инициирует tool-call; исполняет клиент. Все мутирующие tools (`files.write`, `files.mkdir`, `calendar.create_events`, `reminders.create`) → audit-запись. Server-side `site.write_file`/`site.delete` также мутирующие (audit) — см. website-builder.

### Имена tools: доменный (iOS) vs Anthropic-формат
Публичный контракт с iOS (ТЗ §5) использует **доменные имена с точкой** (`files.read`, `calendar.create_events`, …). Anthropic Messages API требует имя tool по шаблону `^[a-zA-Z0-9_-]{1,128}$` — **точка недопустима**, dotted-имя → `400 invalid_request_error` (BUG-3, воспроизведено: dotted→400, underscore→200).

**Решение (без breaking change §5):** ввести двунаправленный маппинг `domain-name (точка) ↔ anthropic-name (подчёркивание)`. Преобразование детерминированное — замена `.`→`_`:

| Domain-name (iOS-facing, публичный) | Anthropic-name (только в Anthropic tool definitions) |
|---|---|
| `files.read` | `files_read` |
| `files.write` | `files_write` |
| `files.list` | `files_list` |
| `files.mkdir` | `files_mkdir` |
| `calendar.read` | `calendar_read` |
| `calendar.create_events` | `calendar_create_events` |
| `reminders.read` | `reminders_read` |
| `reminders.create` | `reminders_create` |
| `maps.show_place` | `maps_show_place` |
| `maps.geocode` | `maps_geocode` |
| `maps.reverse_geocode` | `maps_reverse_geocode` |
| `maps.route` | `maps_route` |
| `maps.search_places` | `maps_search_places` |

**Правила маппинга (нормативно):**
- Маппинг — единственный источник истины для соответствия имён; набор tools фиксирован (по одной паре на каждый инструмент реестра — состав и число см. [§GET /v1/tools](#get-v1tools--каталог-инструментов-adr-019), раздел-первоисточник; таблица выше показывает только client-side пары, server-side `site.*`/`time.now`/`quiz.generate` маппятся тем же правилом `.`→`_`), поэтому маппинг — статическая таблица (двунаправленный dict), а не «слепое» преобразование строк на лету. Обратный маппинг (`anthropic-name → domain-name`) валидирует, что Claude вернул известный tool; неизвестное имя → ошибка обработки tool_use (трактуется как upstream-аномалия, не доходит до iOS).
- При **сборке запроса** к Anthropic (`messages.create`, поле `tools[].name`) backend подставляет **anthropic-name**.
- При **парсинге ответа** Claude (`content` block `type=tool_use`, поле `name`) backend применяет **обратный маппинг** → доменное имя. Наружу — в `toolCall.name` ответов `/v1/chat/run` и `/v1/chat/tool-result`, а также в `tool_calls.tool_name` (БД/audit) — идёт **только доменный формат с точкой**.
- Строгая типизация args/result привязана к **доменным именам** (таблица схем ниже не меняется). Anthropic-имена — исключительно транспортная деталь слоя Anthropic-клиента и нигде, кроме поля `tools[].name`/`tool_use.name` протокола Anthropic, не фигурируют.
- Публичный tool-контракт с iOS (`toolCall.name`, схемы args/result) **не меняется** — это не breaking change.

| Tool | Тип | Args schema | Result schema |
|---|---|---|---|
| `files.read` | read | `{ "path": string }` | `{ "path": string, "content": string, "encoding": "utf8\|base64", "size": int }` |
| `files.write` | mutate | `{ "path": string, "content": string, "encoding": "utf8\|base64", "overwrite": bool }` | `{ "path": string, "bytesWritten": int }` |
| `files.list` | read | `{ "path": string, "recursive": bool }` | `{ "entries": [ { "name": string, "path": string, "isDir": bool, "size": int } ] }` |
| `files.mkdir` | mutate | `{ "path": string, "createIntermediates": bool }` | `{ "path": string, "created": bool }` |
| `calendar.read` | read | `{ "start": "ISO8601 datetime", "end": "ISO8601 datetime", "calendarId": string? }` ([ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md)) | `{ "events": [ { "id": string, "title": string, "start": "ISO8601 datetime", "end": "ISO8601 datetime", "location": string?, "notes": string? } ] }` |
| `calendar.create_events` | mutate | `{ "events": [ { "title": string, "start": "ISO8601 datetime", "end": "ISO8601 datetime", "location": string?, "notes": string?, "calendarId": string? } ] }` | `{ "created": [ { "id": string, "title": string } ] }` |
| `reminders.read` | read | `{ "listId": string?, "includeCompleted": bool }` | `{ "reminders": [ { "id": string, "title": string, "due": "ISO8601"?, "completed": bool, "notes": string? } ] }` |
| `reminders.create` | mutate | `{ "reminders": [ { "title": string, "due": "ISO8601"?, "notes": string?, "listId": string? } ] }` | `{ "created": [ { "id": string, "title": string } ] }` |
| `maps.geocode` ([ADR-102](../../adr/ADR-102-mapkit-client-tools.md)) | read | `{ "query": string(1..200), "maxResults": int(1..10, def 5) }` | `{ "query": string, "places": [ Place ] }` |
| `maps.reverse_geocode` | read | `{ "pointKind": "current_location"\|"coordinates", "latitude": float(-90..90)?, "longitude": float(-180..180)? }` | `{ "places": [ Place ], "locationAccuracy": "precise"\|"reduced"? }` |
| `maps.search_places` | read | `{ "query": string(1..200), "centerKind": "current_location"\|"coordinates", "centerLatitude": float?, "centerLongitude": float?, "radiusMeters": int(100..50000, def 2000), "maxResults": int(1..10, def 5) }` | `{ "query": string, "places": [ Place + distanceText, distanceMeters ], "locationAccuracy": "precise"\|"reduced"? }` |
| `maps.route` | read | `{ "originKind": "current_location"\|"coordinates", "originName": string?, "originLatitude": float?, "originLongitude": float?, "destinationName": string(1..200), "destinationLatitude": float, "destinationLongitude": float, "transportType": "automobile"\|"walking"\|"transit", "departureKind": "now"\|"at", "departureTimeLocal": "ISO8601 datetime (local, без offset)"? }` | `{ "originName": string, "destinationName": string, "transportType": string, "locationAccuracy": …?, "routes": [ { "summaryText", "travelTimeText", "travelTimeSeconds", "distanceText", "distanceMeters", "departureTimeLocal", "arrivalTimeLocal", "trafficAware" } ] }` |
| `maps.show_place` | show | `{ "name": string(1..200), "latitude": float(-90..90), "longitude": float(-180..180) }` | `{ "name": string, "shown": bool }` |

`Place` = `{ "name": string, "address": string, "latitude": float\|null, "longitude": float\|null }` — **имена вперёд, числа следом**. Нормативные правила формы, коды отказа и приватность координат — [§Контракт инструментов карт](#контракт-инструментов-карт-нормативно-adr-102).

### Общие правила схем
- Все схемы — Pydantic v2, `extra='forbid'`.
- Даты — ISO8601 (RFC3339), UTC или с offset. **Исключение (одно, действует в двух семействах):** календарные `start`/`end` (`calendar.read`, `calendar.create_events`) и `departureTimeLocal`/`arrivalTimeLocal` инструментов карт (`maps.route`) — ISO8601-datetime в локальном времени **без** offset (naive local), секундная точность. **Обе стороны помечены намеренно:** это ОДНА конвенция, названная по-разному, — календарные имена (`start`/`end`) уже опубликованы и менять их означало бы breaking change iOS-контракта ([ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md)), а новые поля карт несут конвенцию в имени (`…Local`). Не читать разные имена как разные конвенции. См. «Контракт календарных инструментов: `start`/`end`» и [§Контракт инструментов карт](#контракт-инструментов-карт-нормативно-adr-102) ниже, [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md) / [ADR-102 §4](../../adr/ADR-102-mapkit-client-tools.md).
- `path` валидируется как относительный/безопасный (без `..`-traversal) на стороне валидатора backend; фактический доступ — ответственность клиента.
- `error` (в tool-result) имеет форму `{ "code": string, "message": string }`; при `error` backend передаёт Claude tool_result с `is_error=true`.

### Контракт календарных инструментов: `start`/`end` (нормативно, [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md))
**Единый контракт диапазона для `calendar.read` и `calendar.create_events`** (полная консистентность, [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md)):

- **Имена аргументов диапазона — идентичны:** `start` / `end` в обоих инструментах. `calendar.read` использует `start`/`end` (ранее `startDate`/`endDate` — **переименовано**, breaking change); `calendar.create_events` — `events[].start` / `events[].end` (без изменений имён).
- **Формат значения — идентичен:** ISO8601 **datetime** в **локальном времени без timezone-offset**, секундная точность — например `"2026-06-11T09:00:00"`. **Date-only (`"2026-06-11"`) больше не является целевым контрактом** для `calendar.read` (backward-compat date-only не поддерживается, [ADR-027 §Decision 2](../../adr/ADR-027-calendar-read-contract-alignment.md)). Naive local — это сложившийся де-факто формат `create_events`; read выровнен под него. Tz-aware — возможное будущее усиление обоих ([Q-027-1](../../99-open-questions.md)).
- **Семантика диапазона — end-exclusive:** интервал `[start, end)` — `start` включительно, `end` исключительно. «Весь день D» = `start="D T00:00:00"`, `end="D+1 T00:00:00"` (полночь следующего дня), а **не** `end="D T23:59:59"` ([ADR-027 §Decision 5](../../adr/ADR-027-calendar-read-contract-alignment.md)). Это даёт достижимость диапазона по времени внутри дня (например 09:00–18:00) и однозначность смежных дней.
- **Валидация формата — НЕ серверная:** `start`/`end` — простой `str` в Pydantic-схеме (без datetime-валидации), **симметрично для read и create** ([ADR-027 §Decision 3](../../adr/ADR-027-calendar-read-contract-alignment.md)). Формат доводится до модели через `TOOL_DESCRIPTIONS` (см. ниже), фактический парсинг datetime — на стороне iOS (EventKit), как и подобает client-side tool ([ADR-011](../../adr/ADR-011-server-side-tools.md)).
- **Описание для модели (`TOOL_DESCRIPTIONS`) — самодостаточно по формату.** Описания `calendar.read` и `calendar.create_events` обязаны явно указывать ISO8601-datetime-формат `start`/`end` (local, no offset, пример `"2026-06-11T09:00:00"`) и end-exclusive-конвенцию «весь день», чтобы модель генерировала datetime, а не date-only. **Корень устранённого бага:** ранее формат жил только в docs и не доходил до модели — модель генерировала date-only ([ADR-027 §Context](../../adr/ADR-027-calendar-read-contract-alignment.md)).
- **Breaking change iOS-контракта `calendar.read`** ([ADR-027 §Consequences](../../adr/ADR-027-calendar-read-contract-alignment.md)): iOS-клиент обязан читать args `start`/`end` (не `startDate`/`endDate`) и трактовать значения как datetime. Требуется скоординированный релиз iOS. **Состав** каталога `/v1/tools` этим изменением не затрагивается — меняется только `inputSchema` записи `calendar.read` (генерируется из `_ARGS_BY_TOOL`); число записей каталога здесь не фиксируется, актуальное значение — [§GET /v1/tools](#get-v1tools--каталог-инструментов-adr-019) (раздел-первоисточник). BUG-3 name-map (имена инструментов) не затрагивается — меняются имена **аргументов**, не имя tool.
- **Исторические сессии:** старые `chat_steps`/`tool_calls` хранят прежние `startDate`/`endDate`-вызовы как есть; нормализация истории ([ADR-024](../../adr/ADR-024-history-payload-domain-normalization.md)) не переписывает `tool_use.input`. Миграция не требуется ([Q-027-2](../../99-open-questions.md)).

<a id="контракт-инструментов-карт-нормативно-adr-102"></a>
### Контракт инструментов карт: `maps.*` (нормативно, [ADR-102](../../adr/ADR-102-mapkit-client-tools.md))

Пять **client-side** инструментов ([ADR-011](../../adr/ADR-011-server-side-tools.md)): `maps.show_place`, `maps.geocode`, `maps.reverse_geocode`, `maps.route`, `maps.search_places`. MapKit/CoreLocation живут только на устройстве — бэкенд объявляет и инициирует, приложение исполняет и возвращает результат через `/v1/chat/tool-result`. Схемы args/result — в таблице выше.

- **Результат клиентского инструмента сервер НЕ валидирует** — единственная проверка — размер (`SIZE_LIMIT_TOOL_RESULT`, дефолт 256 KB); содержимое непрозрачно и уходит модели как есть. Следствие, определяющее весь раздел: **всё нормативное ниже обязано доходить до модели схемой args и `TOOL_DESCRIPTIONS`**, а не только этим документом — иначе повторится корень [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md) (норма жила в docs и до модели не доходила).
- **Позиционные пары координат запрещены** — и в аргументах, и в результатах. Только именованные поля (`latitude`/`longitude`, `centerLatitude`/`centerLongitude`, `originLatitude`/`originLongitude`, `destinationLatitude`/`destinationLongitude`); ни `[lon, lat]`, ни `"55.75,37.62"`. Перепутанный порядок «долгота, широта» — самая частая **молчаливая** ошибка области: значения валидны, тип верен, точка в другом полушарии. Диапазоны — ключами схемы (`ge`/`le`), но они ловят только `|latitude| > 90`; от перестановки двух значений ≤ 90 защищают **имена полей**.
- **Числовые ограничения — ключами схемы, не валидатором** ([ADR-065 §4](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)): `maxResults` `1..10`, `radiusMeters` `100..50000`, длины строк, диапазоны координат, наборы `enum`. Иначе модель узнаёт о нарушении лишним оплаченным витком.
- **Явный признак вместо умолчания (нормативно).** `centerKind` / `originKind` / `pointKind` (`current_location` \| `coordinates`) и `departureKind` (`now` \| `at`) — **обязательные** перечисления. Умолчание «нет координат ⇒ взять геопозицию» и «нет времени ⇒ считать на сейчас» запрещено: оно невидимо ни в аргументах, ни в истории, ни в ответе. Ветка `now` разрешается **приложением** (на устройстве локальное время известно точно; `time.now` без `tz` его не даёт), и разрешённое значение возвращается в результате как `departureTimeLocal`.
- **Кросс-полевые правила** (`coordinates` ⇒ обе координаты заданы; `current_location` ⇒ обе пусты; `at` ⇒ задан `departureTimeLocal`) в JSON Schema без `oneOf`/`if-then` не выражаются, а опираться на их поддержку двумя провайдерами контракт не должен (та же причина, что у запрета `$ref`). Поэтому они проверяются валидатором, а все пять инструментов входят в `ARGS_DEGRADE_TOOLS`: нарушение → tool-result error **`invalid_maps_args`**, ход **продолжается**. > **Контраст (не переносить по аналогии):** у инструментов вне `ARGS_DEGRADE_TOOLS` невалидные args по-прежнему дают **`422`** на весь ход. Дополнительно сообщение строится `content_free_args_error`: `str(exc)` у pydantic процитировал бы сами значения, то есть координаты.
- **Форма результата (нормативно).** (1) **Имена мест вперёд, числа следом** — в объекте места первым `name`, затем `address`, затем числовые поля. (2) **Единица измерения — в имени поля** (`distanceMeters`, `travelTimeSeconds`, `radiusMeters`); отдельного поля `unit` нет намеренно — значение и единица в разных полях суть два источника одного факта. (3) **Локализованная строка — отдельным полем** (`distanceText`, `travelTimeText`, `summaryText`, `originLabel`), её формирует **приложение** по локали и системе мер устройства, а модель обязана **процитировать её дословно** и не переводить число сама: она не знает ни локали, ни системы мер (`context.locale` [ADR-037](../../adr/ADR-037-chatrunrequest-context-allowlist-injection.md) необязателен и говорит о языке, а не о мерах: `en-GB` — метрическая страна с милями на дорогах). Числа остаются в результате, чтобы модель сравнивала и сортировала, а не показывала.
- **Пустой результат — НЕ ошибка.** Каждый читающий инструмент возвращает **список** (`places[]`, у `maps.route` — `routes[]` из 0..1 элементов); пустой список = «ничего не нашлось». Ошибка ушла бы провайдеру как `is_error=true`, и модель читала бы её как «повтори» — то есть зацикливалась бы на запросе, который отработал верно. Эхо запроса (`query` / `originName` / `destinationName` / `transportType`) лежит **вне** списка, чтобы пережить пустой результат.
- **Размер результата ограничивают АРГУМЕНТЫ.** Результат персистится и реплеится провайдеру на каждом следующем витке хода и на каждом последующем ходе сессии, то есть оплачивается многократно; сервер его не проверяет, поэтому единственный работающий рычаг — `maxResults` (валидируется). Нормативно: строковые поля ≤ 200 символов, элементов ≤ `maxResults` ⇒ единицы килобайт против `SIZE_LIMIT_TOOL_RESULT` (256 KB) и `SIZE_LIMIT_BODY` (512 KB, под который подпадает `POST /v1/chat/tool-result`).
- **Разрешение на геопозицию ≠ `requiresConfirmation`.** Подтверждение — **на один вызов**, признак задаёт **сервер** (`CONFIRM_TOOLS`), смысл — «выполни именно это действие». Разрешение iOS — **на всё приложение и до отзыва в настройках**, выдаётся вне хода, и сервер его состояния **не знает и узнать не может**. Отсюда: (а) сервер не гейтит инструмент по разрешению — оно проявляется только отказом времени исполнения; (б) выданное разрешение **не** является согласием на конкретный запрос, поэтому «использовать моё место» — явный признак в аргументах; (в) приложение не эскалирует точность и не выпрашивает разрешение повторно ради инструмента чата.
- **Коды отказа (`error.code`) и предписанное моделью поведение** (текст `message` — свободный, контрактом не является):

  | `code` | Когда | Модель обязана |
  |---|---|---|
  | `location_permission_denied` | разрешение отклонено/запрещено настройками | не повторять вызов с `current_location` в этом ходе; попросить назвать место словами и повторить через `maps.geocode` с явными координатами |
  | `location_permission_not_determined` | человек ещё не отвечал на системный запрос | то же; не повторять вызов в надежде, что разрешение появится само |
  | `location_unavailable` | разрешение есть, фикс не получен (помещение, режим полёта, таймаут) | сказать, что положение не определяется, и попросить назвать место; тот же вызов без новых входных данных не повторять |
  | `transport_unavailable` | `transportType` недоступен для этой пары точек/региона (типично `transit`) | не повторять тот же тип; предложить `automobile`/`walking` и спросить |
  | `maps_unavailable` | карты/сеть недоступны на устройстве | ответить словами; не подменять результат выдуманными координатами или расстояниями |

  Поверх таблицы: **повтор того же вызова без новых входных данных запрещён**. Клиентские витки, в отличие от серверных, `MAX_SERVER_TOOL_ROUNDS` не ограничены (счётчик считает раунды, исполняемые бэкендом), поэтому от зацикливания защищает только это предписание в описании инструмента.
- **Огрублённая геопозиция iOS** (`reducedAccuracy`) — **не отказ**: вызов, использовавший геопозицию, возвращает `locationAccuracy` = `"precise"` \| `"reduced"`; при `"reduced"` модель обязана сказать, что положение приблизительно, и не выдавать время в пути за точное. У вызова с явными координатами поля нет — его отсутствие информативно.
- **Приватность координат (инвариант).** **Координаты собственного положения пользователя не появляются ни в аргументах, ни в результатах**: `current_location` резолвит приложение на устройстве, в аргументах стоит значение перечисления, в результате — локализованная подпись и признак точности. У `maps.reverse_geocode` с `pointKind="current_location"` результат несёт адрес, а `latitude`/`longitude` в нём — **`null`** (иначе точный фикс вернулся бы окольным путём). Координаты **мест, о которых спросил человек**, персистятся (`tool_calls.args`, `chat_steps`) и реплеятся провайдеру — это неустранимо и ограничивает утечку районом, а не точкой. Огрубление координат мест отклонено: оно ломает ответ (маршрут и булавка уезжают), не защищая ничего сверх уже сделанного. Подробная сверка поверхностей (audit, логи, CRM, история) — [ADR-102 §9](../../adr/ADR-102-mapkit-client-tools.md) и [05-security.md §Геопозиция](../../05-security.md#геопозиция-и-координаты-инструменты-карт-adr-102).
- **Модель не выдумывает координаты.** `maps.show_place` и `maps.route` требуют координат обязательными полями; получить их полагается через `maps.geocode`/`maps.search_places`. Точка «по памяти» отличается от прочих ошибок тем, что **не даёт ошибки**: маршрут построится, просто не туда.
- **`transportType` не содержит велосипеда** — `MKDirectionsTransportType` его не поддерживает; значение перечисления, неисполнимое приложением, — гарантированный отказ, оформленный как возможность.

### blockReason enum (повтор для удобства)
`trial_used | subscription_required | subscription_expired | credits_empty | byok_disabled | byok_invalid | rate_limited | policy_denied | max_tokens` (источник — [ADR-004](../../adr/ADR-004-blocked-http-200.md); `max_tokens` добавлен [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) — обрезка ответа по лимиту output-токенов, в отличие от прочих policy-причин срабатывает **после** начала генерации: `usage`/`messageStepId`/`stepId` присутствуют, кредит не списывается).

---

## GET /v1/tools — каталог инструментов ([ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md))
Машиночитаемый каталог всех поддерживаемых backend tools (**37**, включая `time.now` [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md), `quiz.generate` [ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md), media generate/ask_params [ADR-068](../../adr/ADR-068-media-generate-chat-tools.md)/[ADR-070](../../adr/ADR-070-media-choices-wizard.md), `maps.*` [ADR-102](../../adr/ADR-102-mapkit-client-tools.md)). Источник — `src/app/chat/tools.py` (single source of truth: `_ARGS_BY_TOOL`, `MUTATING_TOOLS`, `SERVER_SIDE_TOOLS`, `GLOBAL_SERVER_SIDE_TOOLS`, `TOOL_GENERATION_MODES`, `anthropic_tool_definitions()`). Эндпоинт **не** параметризуется ни `assistantMode`, ни наличием проекта, ни `generationMode`. Оси A/B/C/D/E каталог не режут: `quiz.generate` в каталоге **всегда**, хотя модели предлагается только в `study_learn`; `maps.*` в каталоге **всегда**, хотя предлагаются только при `MAPS_TOOLS_ENABLED=true` (ось E, [ADR-102 §10](../../adr/ADR-102-mapkit-client-tools.md)). Исключение — per-instance denylist [ADR-081](../../adr/ADR-081-disabled-tool-families.md) (`CHAT_DISABLED_TOOL_FAMILIES`): на инстансе с заданным списком семейства (`files`/`calendar`/`reminders`/`site`) **отсутствуют** и в `GET /v1/tools`, и в offer-set модели. Пустой дефолт = полный реестр (все инстансы кроме явно настроенных). **`maps` в `DISABLEABLE_TOOL_FAMILIES` намеренно НЕ входит** ([ADR-102 §10](../../adr/ADR-102-mapkit-client-tools.md)): денилист — механизм opt-out (по умолчанию семейство предлагается везде), а карты обязаны быть по умолчанию **выключены** — их гейтит собственный флаг `MAPS_TOOLS_ENABLED`, и он, в отличие от денилиста, каталог не режет.

### Auth
- **JWT-protected** (как все `/v1/*`, кроме `/v1/preview/*`): `Authorization: Bearer <JWT>` обязателен. Каталог не секретен, но единообразие gateway-auth и снижение анонимного API-surface — обоснование в [ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md). Клиент к этому моменту уже имеет JWT (получен через `/v1/auth/register`, [ADR-018](../../adr/ADR-018-embedded-auth-issuer.md)).
- Метод `GET` (read-only, кэшируемо). Per-user rate-limit как у прочих read-эндпоинтов.

### Response (200)
```json
{
  "tools": [
    {
      "name": "files.read",
      "description": "Read a file from the user's device.",
      "mutating": false,
      "execution": "client",
      "inputSchema": { "type": "object", "properties": { "path": { "type": "string" } }, "required": ["path"] },
      "requiresConfirmation": false
    },
    {
      "name": "site.write_file",
      "description": "Write or overwrite a file in the website project...",
      "mutating": true,
      "execution": "server",
      "inputSchema": { "type": "object", "properties": { "...": {} } },
      "requiresConfirmation": true
    }
  ]
}
```
- `name` — **доменное** имя с точкой (как в публичном iOS-контракте), НЕ anthropic-underscore (`files_read` — деталь Anthropic-транспорта, BUG-3).
- `description` — из `descriptions` в `anthropic_tool_definitions()`.
- `mutating` — `name ∈ MUTATING_TOOLS` (требует audit при исполнении). **Не признак подтверждения** — для этого есть отдельное поле ниже.
- **`requiresConfirmation` — спрашивать ли пользователя перед исполнением вызова (нормативно, [ADR-094 §4](../../adr/ADR-094-code-assistant-tools.md)).** `name ∈ CONFIRM_TOOLS`. Признак задаёт **бэкенд**, и клиенту запрещено выводить его из имени инструмента: вывод ломается сразу (`files.search` только читает, `git.branch` меняет состояние, оба начинаются с «безопасного» префикса), и приложение исполнило бы без диалога то, что спрашивать полагалось. То же поле дублируется в каждом `toolCall` ответа `/v1/chat/run` — каталог нужен, чтобы отрисовать «Всегда доверять» **до** первого вызова.
  - **Читающие инструменты подтверждения не требуют намеренно** (`requiresConfirmation=false` при `mutating=false`): диалог на каждый просмотр файла приучает нажимать «да» не глядя и обесценивает единственный диалог, который важен — перед `git.push` с перезаписью истории.
  - Множества `mutating` и `requiresConfirmation` **не совпадают**: `document.create`/`document.update` меняют данные на **бэкенде** (подтверждать нечего — пользователь уже попросил), а подтверждения требуют только вызовы, исполняемые на машине пользователя.
- `execution` — `"server"` если `name ∈ SERVER_SIDE_TOOLS ∪ GLOBAL_SERVER_SIDE_TOOLS` (`site.*` — [ADR-011](../../adr/ADR-011-server-side-tools.md); `time.now` — [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md); исполняет backend); иначе `"client"` (исполняет iOS).
<a id="inputschema--нормативный-формат"></a>
- **`inputSchema` — JSON Schema аргументов инструмента (нормативно).** Строится из `model_json_schema()` модели args, из которой **вырезана модельная метаинформация**: корневые `title` (= имя Python-класса) и `description` (= docstring класса), а у инструментов с self-contained-схемой ([§`quiz.generate`](#quizgenerate--server-side-global-tool-режимный-adr-064)) — те же два ключа и у **инлайненных** определений вложенных моделей. **Сохраняются:** пофилдовые `title`/`description` (из `Field(...)`), `type`/`properties`/`items`/`required`/`enum`/`additionalProperties` и ограничивающие ключи (`minItems`/`maxItems`/`maxLength`/…). Формат **не** определяется как «сырой вывод `model_json_schema()`»: равенство сырому выводу нарушало бы инвариант ниже.
  - **Инвариант «внутренние идентификаторы не покидают процесс» (нормативно, шире этого поля).** Ни один артефакт, уходящий наружу — `inputSchema` в `GET /v1/tools`, `description` записи каталога (`TOOL_DESCRIPTIONS`), `input_schema`/`parameters` и `description`, уходящие **провайдеру** — не должен содержать внутренних идентификаторов разработки: ссылок `ADR-NNN`/`TD-NNN`/`Q-NNN-N`/`BUG-N`, имён внутренних классов (`*Args`, `GlobalToolHandlers`, `SiteToolHandlers`), имён внутренних констант/реестров (`MAX_SERVER_TOOL_ROUNDS`, `_ARGS_BY_TOOL`). Это та же норма, что [08-api-documentation.md §R2ter](../../08-api-documentation.md) предъявляет к user-facing текстам OpenAPI, распространённая на **вторую** поверхность утечки — tool-контракт: docstring внутренней модели, попавший в схему, уезжает и клиенту, и в промпт модели.
  - **Способ соблюдения — вырезание на границе, а не дисциплина docstring'ов.** Требование адресовано **генератору схемы** (одна точка, `tool_input_schema`), а не авторам моделей: правило «не писать ADR-ссылок в docstring» не проверяемо и ломается первым же новым инструментом. Docstring'и внутренних моделей остаются нормальной внутренней документацией.
  - **Покрытие — тест-детектор, а не ревью глазами:** скан **всех** записей каталога и **всех** определений, уходящих провайдеру, регуляркой на перечисленные классы идентификаторов → ноль совпадений; плюс проверка, что пофилдовые описания при этом **не** пусты (вырезание не должно выкосить полезную часть). См. [09-testing.md](09-testing.md#unit--каталог-инструментов-и-утечка-внутренних-идентификаторов).
- Порядок — детерминированный (по `_ARGS_BY_TOOL`).

### Полный список (37)
| name | execution | mutating | confirm |
|---|---|---|---|
| files.read | client | нет | нет |
| files.write | client | **да** | **да** |
| files.list | client | нет | нет |
| files.mkdir | client | **да** | **да** |
| files.delete | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | **да** | **да** |
| files.move | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | **да** | **да** |
| files.search | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | нет | нет |
| files.patch | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | **да** | **да** |
| git.status | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | нет | нет |
| git.diff | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | нет | нет |
| git.log | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | нет | нет |
| git.commit | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | **да** | **да** |
| git.branch | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | **да** | **да** |
| git.push | client ([ADR-094](../../adr/ADR-094-code-assistant-tools.md), ось D) | **да** | **да** |
| calendar.read | client | нет | нет |
| calendar.create_events | client | **да** | нет |
| reminders.read | client | нет | нет |
| reminders.create | client | **да** | нет |
| site.write_file | **server** | **да** | нет |
| site.preview | **server** | нет | нет |
| site.list | **server** | нет | нет |
| site.read | **server** | нет | нет |
| site.delete | **server** | **да** | нет |
| time.now | **server** (global, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)) | нет | нет |
| quiz.generate | **server** (global, режимный, [ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) | нет | нет |
| media.generate_image | **server** (global, [ADR-068](../../adr/ADR-068-media-generate-chat-tools.md)) | нет | нет |
| media.generate_video | **server** (global, [ADR-068](../../adr/ADR-068-media-generate-chat-tools.md)) | нет | нет |
| media.ask_params | **server** (global, [ADR-068](../../adr/ADR-068-media-generate-chat-tools.md)) | нет | нет |
| document.create | **server** (global, [ADR-090](../../adr/ADR-090-chat-documents.md)) | **да** | нет |
| document.list | **server** (global, [ADR-090](../../adr/ADR-090-chat-documents.md)) | нет | нет |
| document.read | **server** (global, [ADR-090](../../adr/ADR-090-chat-documents.md)) | нет | нет |
| document.update | **server** (global, [ADR-090](../../adr/ADR-090-chat-documents.md)) | **да** | нет |
| maps.show_place | client ([ADR-102](../../adr/ADR-102-mapkit-client-tools.md), ось E) | нет | нет |
| maps.geocode | client ([ADR-102](../../adr/ADR-102-mapkit-client-tools.md), ось E) | нет | нет |
| maps.reverse_geocode | client ([ADR-102](../../adr/ADR-102-mapkit-client-tools.md), ось E) | нет | нет |
| maps.route | client ([ADR-102](../../adr/ADR-102-mapkit-client-tools.md), ось E) | нет | нет |
| maps.search_places | client ([ADR-102](../../adr/ADR-102-mapkit-client-tools.md), ось E) | нет | нет |

> **Порядок записей** — по `_ARGS_BY_TOOL`; пять инструментов карт добавляются **в конец** реестра, поэтому порядок уже опубликованных записей не меняется.
>
> **Инструменты карт не мутируют и подтверждения не требуют** ([ADR-102 §1](../../adr/ADR-102-mapkit-client-tools.md)): они читают справочник и показывают карточку, ничего на устройстве не меняя. Разрешение iOS на геопозицию — **не** `requiresConfirmation`: это разрешение уровня ОС на всё приложение, сервер о нём не знает и знать не может (см. [§Контракт инструментов карт](#контракт-инструментов-карт-нормативно-adr-102)).

> **Global server-side tools** (`time.now`, `quiz.generate`): `execution=server`, но в отличие от `site.*` **не требуют проекта**. Предложение модели внутри класса различается: `time.now` — всегда; `quiz.generate` — только при `generationMode=study_learn` (ось C). domain↔anthropic: `time.now ↔ time_now`, `quiz.generate ↔ quiz_generate`.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.

## GET /v1/models — список доступных моделей инстанса ([ADR-034](../../adr/ADR-034-user-model-selection.md) / [ADR-073](../../adr/ADR-073-dual-credits-llm-providers.md) / [ADR-075](../../adr/ADR-075-unified-instance-models-catalog.md))

Источник для селектора. Возвращает **всё, что инстанс умеет обслужить**: chat-модели credits-провайдеров + fal photo/video, если задан `FAL_API_KEY`. Chat без `LLM_PROVIDERS` — только активный `LLM_PROVIDER`; с opt-in `LLM_PROVIDERS` — union allowlist'ов обоих, у которых задан API key. Leftover-ключ соседнего LLM dual **не** включает. Пустой `FAL_API_KEY` — fal-строк нет.

### Auth
- **JWT-protected** (как `GET /v1/tools`, [ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md)): `Authorization: Bearer <JWT>` обязателен. Список не секретен, контур авторизации единый. Per-user rate-limit как у прочих read-эндпоинтов (`enforce_other_limits`). Метод `GET` (read-only, кэшируемо).

### Response (200)
Обёртка `{models:[…]}` сохранена (не raw-массив). Поля `name` / `modality` / `variant` / `family` / `provider=fal` — **аддитивные**.
```json
{
  "models": [
    { "id": "gpt-4.1", "displayName": "GPT-4.1", "name": "GPT-4.1", "default": true, "provider": "openai", "modality": "chat", "variant": null, "family": null },
    { "id": "gpt-4o", "displayName": "GPT-4o", "name": "GPT-4o", "default": false, "provider": "openai", "modality": "chat", "variant": null, "family": null },
    { "id": "fal-ai/nano-banana-pro", "displayName": "Nano Banana Pro", "name": "Nano Banana Pro", "default": true, "provider": "fal", "modality": "photo", "variant": "Text to Image", "family": "Nano-Banana-Pro" },
    { "id": "fal-ai/veo3.1", "displayName": "Veo 3.1", "name": "Veo 3.1", "default": false, "provider": "fal", "modality": "video", "variant": "Text to Video", "family": "veo3.1" }
  ]
}
```
- `id` — для `modality=chat` уходит в `POST /v1/chat/run` `model`. Для photo/video — endpoint fal; в `chat.model` **не** принимается (`422 unsupported_model`).
- `displayName` / `name` — одно и то же человекочитаемое имя (`name` — дубль для клиентов, которые читают `name`).
<a id="default-per-modality-adr-087"></a>
- **`default` (bool) трактуется ТОЛЬКО внутри `modality` ([ADR-075](../../adr/ADR-075-unified-instance-models-catalog.md), уточнено [ADR-087 §4](../../adr/ADR-087-default-chat-model-gpt-4-1.md)).** Клиент обязан **сначала отфильтровать по `modality`**, и только потом читать `default`:
  - `modality=chat` — ровно один `true` (дефолт инстанса), он **первый** в массиве;
  - `modality=photo` — ровно один `true` (`fal-ai/nano-banana-pro`), только когда задан `FAL_API_KEY`;
  - `modality=video` — **всегда `false`**: дефолтной видео-модели у инстанса нет.

  Отсюда: в одном ответе одновременно присутствуют **до двух** `default: true` (chat и photo) — это контракт, а не дефект. Прочтение «ровно один `default: true` на весь ответ» неверно и было источником репорта BUG-003.
- `provider` (`openai`\|`anthropic`\|`fal`) — аддитивное поле. Старые клиенты игнорируют неизвестные ключи.
- **`modality` (`chat`\|`photo`\|`video`) — стабильный машиночитаемый фильтр ([ADR-087 §5](../../adr/ADR-087-default-chat-model-gpt-4-1.md)):** существующие значения не переименовываются и не меняют смысла; расширение набора возможно только новым ADR и только добавлением значения; клиент обязан **игнорировать** строку с неизвестной ему `modality`, а не падать на ней. Селектор чата берёт только `chat`.
- `variant` / `family` — режим и семейство fal; у chat всегда `null`.
- **Пустой chat-allowlist** ⇒ дефолт инстанса первым + встроенный продуктовый каталог провайдера ([ADR-076](../../adr/ADR-076-builtin-chat-product-catalog.md)). Env allowlist добавляет extras и может переименовать; встроенные id не прячет.
- **Смена модели и провайдера внутри чата не поддерживается** (resume игнорирует `model`, [ADR-087 §3](../../adr/ADR-087-default-chat-model-gpt-4-1.md)): выбранная модель фиксируется на сессию, для другой модели — новый чат. `GET /v1/media/models` (короткие id, `modes[]`, цены) не заменяется.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.

## GET /v1/presets — пресеты промтов ([ADR-035](../../adr/ADR-035-prompt-presets-endpoint.md))

Источник для чипов-пресетов на главном экране чата iOS (экран 4). Тап по чипу подставляет `prompt` в композер. Набор и тексты меняются деплоем backend **без релиза iOS-приложения**. Провайдер/инстанс-агностично: идентичный ответ на всех действующих инстансах. Источник — статический реестр в коде (`src/app/chat/presets.py`, single source of truth, по образцу [`GET /v1/tools`](#get-v1tools--каталог-инструментов-adr-019)).

### Auth
- **JWT-protected** (как `GET /v1/tools`/`GET /v1/models`): `Authorization: Bearer <JWT>` обязателен. Каталог не секретен, контур авторизации единый. Per-user rate-limit как у прочих read-эндпоинтов (`enforce_other_limits`). Метод `GET` (read-only, без побочных эффектов: не создаёт сессию, не пишет ledger/audit).

### Query-параметры (локализация, [ADR-049](../../adr/ADR-049-presets-localization.md))
- **`locale` (опц., str).** Явный выбор локали каталога. Допустимый набор — поддерживаемые локали (`en`, `ru`, `zh-Hans`; расширяется). Канонизируется (`zh-Hans` / `zh_Hans` / `zh-CN` → `zh-Hans`, `ru-RU` → `ru`). **Явно указанное значение вне набора → `422`** (`"locale '<x>' is not supported"`) — симметрично строгому `422 unsupported_model` ([ADR-034 §3](#model-опц-session-fixed-adr-034)); молчаливой подмены явного запроса нет.

### Резолвинг локали (порядок, [ADR-049 §3](../../adr/ADR-049-presets-localization.md))
Первый сработавший шаг выигрывает:
1. **Query `?locale=`** — валидное значение из набора; невалидное → `422` (см. выше).
2. **`Accept-Language`** (если query нет) — первый **поддерживаемый** тег: значение делится по `,`, отбрасывается `;q=...`, канонизируется (`zh-Hans-CN`→`zh-Hans`, `ru-RU`→`ru`, `en-US`→`en`); первый тег из набора — результат. Ни одного поддерживаемого / пусто / нераспознано → **тихий fallback** к шагу 3 (заголовок не строго клиент-контролируем → без `422`).
3. **`PRESETS_DEFAULT_LOCALE`** — per-instance дефолт (env, [07-deployment.md](../../07-deployment.md#конфигурация-env); avelyra=`ru`, остальные=`en`). Значение env вне набора → graceful fallback `en` + WARNING (не startup-crash).
4. **`en`** — финальный fallback (канон).

### Response (200)
```json
{
  "locale": "ru",
  "presets": [
    {
      "id": "plan_week",
      "title": "Планирование недели",
      "icon": "calendar",
      "prompt": "Помоги спланировать предстоящую неделю. Расспроси меня о приоритетах, сроках и обязательствах, а затем предложи сбалансированное расписание по дням.",
      "category": "life",
      "subcategory": "planner",
      "description": "Планирует неделю по приоритетам"
    },
    {
      "id": "editor",
      "title": "Редактор",
      "icon": "pencil",
      "prompt": "Ты редактор. Улучши текст, который я пришлю: ясность, тон и структуру, не меняя смысл. Я вставлю черновик следующим сообщением.",
      "category": "work",
      "subcategory": "editor",
      "description": "Улучшает тексты, письма и документы"
    }
  ]
}
```
- `locale` ([ADR-049 §5](../../adr/ADR-049-presets-localization.md), **аддитивно**) — фактически отданная локаль (из поддерживаемого набора; результат резолвинга). Старые клиенты игнорируют поле.
- `id` — стабильный slug (`[a-z0-9_]`, snake_case), уникален в наборе; стабилен между релизами. **Не локализуется** (общий для всех локалей).
- `title` — отображаемое имя чипа (на выбранной локали).
- `icon` — имя **SF Symbol** (например `calendar`, `doc.text`, `camera`); клиент рендерит `Image(systemName:)`, при отсутствии символа — клиентский fallback. Не emoji ([ADR-035 §4](../../adr/ADR-035-prompt-presets-endpoint.md)). **Не локализуется** (стабильный ресурс iOS).
- `prompt` — plain-text (на выбранной локали), подставляется в композер при тапе (без шаблонов/плейсхолдеров на старте).
- `category` ([ADR-080](../../adr/ADR-080-preset-categories.md) / [ADR-083](../../adr/ADR-083-preset-subcategories-and-descriptions.md), **аддитивно**) — жанр карточки на экране агентов. Стабильный slug, **не локализуется**: `work` (работа), `life` (жизнь), `entertainment` (развлечения). На отгружаемом каталоге всегда заполнен, включая исходные семь чипов. Старые клиенты игнорируют поле (Swift `JSONDecoder` по умолчанию пропускает неизвестные ключи).
- `subcategory` ([ADR-083](../../adr/ADR-083-preset-subcategories-and-descriptions.md), **аддитивно**) — карточка агента (`editor` / `letters` / …). На агенте совпадает с `id`; чип указывает на ближайшую карточку. Экран «Агенты» = `id == subcategory` (18 карточек). Не локализуется.
- `description` ([ADR-083](../../adr/ADR-083-preset-subcategories-and-descriptions.md), **аддитивно**) — однострочная подпись карточки на локали ответа (per-field EN-fallback).
- Порядок элементов = порядок чипов на экране (детерминированный, порядок объявления в реестре) — **един во всех локалях**. Поля `id`/`title`/`icon`/`prompt`/`description` обязательны и непусты; `category`/`subcategory` на отгружаемом каталоге всегда заполнены.
- **Per-field EN-fallback:** если у выбранной локали не заполнено какое-то поле пресета, оно берётся из EN (канон); незаполненная/неизвестная локаль целиком → EN-каталог.

**Дефолтный набор (7, со скрина):** `plan_week`, `meeting_notes`, `tasks_from_photo`, `design_brief`, `daily_review`, `summarize_text`, `project_structure` — EN-тексты в [ADR-035 §3](../../adr/ADR-035-prompt-presets-endpoint.md), RU-тексты в [ADR-049 §1.1](../../adr/ADR-049-presets-localization.md). У семерых есть `category`/`subcategory` ([ADR-083](../../adr/ADR-083-preset-subcategories-and-descriptions.md)); это чипы главного экрана (`id != subcategory`).

**Агенты (18, после семёрки, [ADR-080](../../adr/ADR-080-preset-categories.md) / [ADR-083](../../adr/ADR-083-preset-subcategories-and-descriptions.md)):** `work` — `editor`, `letters`, `analyst`, `ideas`, `code`, `documents`; `life` — `finances`, `advisor`, `planner`, `studies`, `translator`, `health`; `entertainment` — `creator`, `movies`, `quizzes`, `companion`, `stories`, `games`. Новый клиент: вкладки по `category`, сетка агентов = `id == subcategory`; старый продолжает читать только `id`/`title`/`icon`/`prompt`.

**Совместимость:** без env и без запроса локали (`locale` отсутствует, `Accept-Language` без поддерживаемых, дефолт `en`) → EN-ответ как раньше; поле `locale` при этом = `"en"`. Без миграции; провайдер-агностично ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)).

**Коды:** `200`; `401` нет/невалидный JWT; `422` явный `?locale=` вне набора; `429` rate-limit.

## GET /v1/characters — каталог персонажей ([ADR-097](../../adr/ADR-097-character-personas.md))

Источник для экрана выбора персонажа. Тап по карточке передаётся в `characterId` при создании чата ([§`characterId`](#characterid-опц-session-fixed-adr-097)). Набор и тексты меняются деплоем backend **без релиза iOS-приложения**. Источник — статический реестр в коде (`src/app/chat/characters.py`, single source of truth, по образцу [`GET /v1/presets`](#get-v1presets--пресеты-промтов-adr-035)).

### Auth
- **JWT-protected** (как `GET /v1/tools` / `GET /v1/models` / `GET /v1/presets`): `Authorization: Bearer <JWT>` обязателен. Метод `GET`, read-only, без побочных эффектов (не создаёт сессию, не пишет ledger/audit). Per-user rate-limit как у прочих read-эндпоинтов.

### Query-параметры (локализация)
- **`locale` (опц., str)** — тот же набор и та же канонизация, что у пресетов (`en`, `ru`, `zh-Hans`; `ru-RU`→`ru`, `zh-CN`→`zh-Hans`). Явное значение вне набора → **`422`** (`"locale '<x>' is not supported"`).

### Резолвинг локали
Порядок **тот же**, что у `GET /v1/presets` ([ADR-049 §3](../../adr/ADR-049-presets-localization.md)), и та же per-instance переменная: `?locale=` → `Accept-Language` (тихий fallback) → **`PRESETS_DEFAULT_LOCALE`** → `en`. Второй env под язык каталога персонажей **не заводится** — язык каталогов инстанса один. Имя переменной шире своей области действия — [TD-035](../../100-known-tech-debt.md).

### Response (200)
```json
{
  "enabled": true,
  "locale": "ru",
  "characters": [
    { "id": "anime_girl",     "name": "Аниме-девушка",   "tagline": "Восторженная героиня аниме",     "icon": "sparkles" },
    { "id": "fantasy_queen",  "name": "Королева фэнтези","tagline": "Церемонная правительница",       "icon": "crown" },
    { "id": "vampire_lord",   "name": "Лорд вампиров",   "tagline": "Древний аристократ ночи",        "icon": "moon.stars" },
    { "id": "cyber_assassin", "name": "Кибер-ассасин",   "tagline": "Немногословный оперативник",     "icon": "bolt.shield" },
    { "id": "virtual_friend", "name": "Виртуальный друг","tagline": "Тёплый повседневный собеседник", "icon": "person.wave.2" }
  ]
}
```
- `enabled` — включена ли фича на этом инстансе (`CHARACTERS_ENABLED`). **`false` → `characters: []`** и `characterId` при создании чата отклоняется (`422 characters_disabled`). Клиент прячет вход в выбор персонажа по этому полю.
- `locale` — фактически отданная локаль (результат резолвинга), как в `GET /v1/presets`.
- `id` — стабильный slug (`[a-z0-9_]`), **не локализуется**: ключ сессии (`chat_sessions.character_id`), ключ клиентской графики и аналитики.
- `name` / `tagline` — отображаемое имя и однострочная подпись на выбранной локали. **Per-field EN-fallback** (EN — канон): незаполненное поле локали берётся из EN. `zh-Hans` на старте не заполнен и целиком приходит по fallback.
- `icon` — имя **SF Symbol** (клиент рендерит `Image(systemName:)`, при отсутствии символа — свой fallback). Не emoji, **не локализуется** (стабильный ресурс iOS).
- Порядок элементов = порядок на экране (порядок объявления в реестре), **един во всех локалях**.
- **Системного текста персонажа (`persona`) в ответе нет** и не будет ([ADR-097 §3](../../adr/ADR-097-character-personas.md)): это внутренняя инструкция модели, которую правят по наблюдениям за ответами; отдав её, мы сделали бы правку формулировки релизом приложения.

**Состав (5, закрыт):** `anime_girl`, `fantasy_queen`, `vampire_lord`, `cyber_assassin`, `virtual_friend` — расширение набора только новым решением.

**Совместимость:** эндпоинт аддитивен; на инстансе без `CHARACTERS_ENABLED` отвечает `200` с пустым списком (а не `404` — иначе приложение не отличит «инстанс не умеет» от «бэкенд старее фичи», [ADR-097 §7](../../adr/ADR-097-character-personas.md)). Без миграции; провайдер-агностично ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)).

**Коды:** `200`; `401` нет/невалидный JWT; `422` явный `?locale=` вне набора; `429` rate-limit.

---

<a id="post-v1chatspeech--озвучка-ответа-adr-100"></a>
## POST /v1/chat/speech — озвучка ответа ([ADR-100](../../adr/ADR-100-assistant-speech-output.md))

Синтез речи по **уже существующему** assistant-шагу. Ход чата этой ручкой не выполняется и не изменяется: она читает сохранённый шаг, приводит его текст к произносимому виду и возвращает звук.

**Контракт хода не меняется ни на байт.** Ни `ChatResponse`, ни SSE-кадры `/v1/chat/v2/run/stream` не получают новых полей ([ADR-100 §1](../../adr/ADR-100-assistant-speech-output.md)).

### Auth
- **JWT-protected**, как прочие `/v1/*`. `userId` обязан совпадать с `sub` (иначе `403`, общее правило [api-gateway](../api-gateway/02-api-contracts.md)).
- Собственный бакет rate-limit: `TTS_RATE_LIMIT_PER_MIN` (дефолт 10/мин на пользователя) — он единственная защита от того, что бесплатный для пользователя повтор (см. идемпотентность ниже) превращается в неограниченное обращение к платному поставщику ([Q-100-2](../../99-open-questions.md)).

### Request
```json
{ "userId": "uuid", "sessionId": "uuid", "stepId": "uuid" }
```
- `StrictModel`, `extra='forbid'`.
- **`stepId`** — `chat_steps.id` **assistant**-шага сессии; то же значение, что клиент получил в `ChatResponse.stepId` ([ADR-023](../../adr/ADR-023-sync-ids-in-chat-response.md)) и видит в `GET /v1/chats/{id}` → `steps[].id`. Адресуется **шаг, а не «последний ответ»**: на гонке двух ходов «последний» разошёлся бы с тем, на что нажал пользователь.
- **`voiceId` не принимается.** Голос выбирает сервер ([§Резолв голоса](03-architecture.md#резолв-голоса-adr-100)). Приняв его от клиента, мы отдали бы приложению право озвучить персонажа чужим голосом — прямо против решения владельца «голоса персонажей закреплены за персонажами и не меняются».

### Response (200)
```json
{
  "stepId": "uuid",
  "voiceId": "char_vampire_lord",
  "mediaType": "audio/mpeg",
  "audio": "<base64>",
  "truncated": false,
  "creditsCharged": 1
}
```
- **`audio`** — base64 синтезированного файла целиком. Клип ограничен потолком `TTS_MAX_CHARS` (см. ниже), поэтому размер ответа ограничен сверху тем же числом, что и счёт поставщика. Транспорт — тот же inline base64, которым аудио приходит **в** сервис ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), [ADR-095](../../adr/ADR-095-voice-messages.md)); цена — +33 % байт, названа в [ADR-100 §2](../../adr/ADR-100-assistant-speech-output.md).
- **`voiceId`** — фактически использованная запись реестра голосов. Потребитель — **ключ клиентского кэша**: приложение хранит звук по паре `(stepId, voiceId)` и после смены голоса в настройках запрашивает шаг заново, получая новую пару. Без этого поля клиент не смог бы отличить свой закэшированный звук старого голоса от актуального.
- **`mediaType`** — MIME синтезированного файла, выводится из `TTS_AUDIO_FORMAT` (`mp3` → `audio/mpeg`, `aac` → `audio/aac`). Клиент по нему выбирает декодер; поле не константа именно потому, что формат — переменная инстанса.
- **`truncated`** — сработал ли потолок длины. `true` → приложение показывает, что прозвучал не весь ответ (текст при этом полный и не изменён).
- **`creditsCharged`** — сколько кредитов реально списал **этот** вызов. На идемпотентном повторе — **`0`**: пользователь второй раз не платит, и поле говорит правду о том, что произошло с балансом (образец — `mediaJobs[].creditsCharged`, [ADR-068](../../adr/ADR-068-media-generate-chat-tools.md); та же семантика «величина ВЫЗОВА» задана образцу нормой в [ADR-103 §3](../../adr/ADR-103-media-jobs-turn-scoped-merge.md) — у восстановленной записи `0`). Клиент по нему обновляет баланс.
- Полей `durationSeconds` / `sanitized` **нет намеренно**: длительность сервер не вычисляет (поставщик её не сообщает, а декодировать mp3 ради поля незачем — клиент узнаёт её при загрузке файла), а признак «что-то вырезано» не имеет потребителя, отличного от `truncated`.

### Правила
- **Приведение к произносимому виду и потолок — на сервере, при чтении** ([§Приведение к произносимому виду](03-architecture.md#приведение-к-произносимому-виду-adr-100)). Порядок обязателен: **сначала чистка, потом потолок**. `chat_steps.payload` не изменяется — история и реплей провайдеру остаются прежними ([ADR-021](../../adr/ADR-021-deterministic-step-order-and-block-normalization.md)).
- **Тарификация — отдельная от хода**, `TTS_CREDIT_COST` (дефолт 1). Порядок: проверка баланса → чистка и потолок → синтез → **при успехе** списание в той же транзакции запроса. Провал поставщика ⇒ списания не было ⇒ ветки возврата не существует. **Контраст с [ADR-060 §4](../../adr/ADR-060-media-generation-fal.md)** (там списание **до** сабмита и обязательный возврат `media-refund:{jobId}`, потому что работа уходит в очередь fal): правило одной поверхности на другую не переносить.
- **Идемпотентность — ключ `tts:{stepId}:{voiceId}`** (`ux_ledger_idempotency`, [ADR-005](../../adr/ADR-005-idempotency-ledger.md)). Повтор той же пары бесплатен **навсегда** — леджер и есть постоянный признак «за этот ответ этим голосом уже заплачено» (переустановка приложения, второе устройство, сетевой ретрай). **Смена голоса — другая пара, другое списание.**
- **Исхода «кредит списан, звук не доставлен» не существует по построению:** списание идёт после успешного синтеза, а если ответ не дошёл по сети — повторный запрос отдаёт звук бесплатно.
- **BYOK и trial платят за озвучку внутренними кредитами** — в отличие от хода, который на BYOK внутренних кредитов не тратит: синтез в любом случае выполняется **нашим** ключом OpenAI ([ADR-044](../../adr/ADR-044-multi-provider-byok.md) — ключ пользователя может быть от другого провайдера). Обе стороны помечены, правило одного пути на другой не переносить.
- **Policy Engine на этом пути не вызывается**, подписка не требуется — гейт только балансовый (как у генерации медиа, [ADR-060 §Границы](../../adr/ADR-060-media-generation-fal.md)): решать, «можно ли сгенерировать ответ», нечего — ответ уже сгенерирован и оплачен.
- **Звук на сервере не хранится** (ни таблицы, ни диска, ни кэша в процессе). Кэш — на клиенте, по паре `(stepId, voiceId)`.

### Коды
| Код | `error.code` | Условие |
|---|---|---|
| `200` | — | звук отдан |
| `401` | `unauthorized` | нет/невалидный JWT |
| `403` | `forbidden` | `userId ≠ sub` |
| `404` | `session_not_found` | сессия не найдена или принадлежит другому пользователю |
| `404` | `step_not_found` | шага нет в этой сессии (ответ удалён вместе с чатом или срезан правкой сообщения, [ADR-040](../../adr/ADR-040-edit-message-and-regenerate.md)). Отдельный код от `session_not_found` намеренно: клиент по нему **убирает кнопку воспроизведения у конкретного сообщения**, а не закрывает чат |
| `409` | `insufficient_credits` | баланса не хватает на `TTS_CREDIT_COST` (проверка **до** синтеза) |
| `422` | `voice_output_disabled` | `VOICE_OUTPUT_ENABLED=false` на инстансе |
| `422` | `nothing_to_speak` | после чистки текста не осталось: ответ целиком из кода, ход с квизом, шаг без текста. Отдавать тишину нельзя — она неотличима от зависшего плеера ([ADR-095 §7](../../adr/ADR-095-voice-messages.md) отказался отвечать пустотой по тому же основанию) |
| `429` | `rate_limited` | превышен `TTS_RATE_LIMIT_PER_MIN` |
| `502` | `upstream_error` | поставщик синтеза отказал; текст исключения наружу не пробрасывается |
| `503` | `voice_output_not_configured` | `OPENAI_API_KEY` пуст — мис-конфигурация инстанса, отличимая от штатно выключенной фичи (`422`) |
| `504` | `gateway_timeout` | таймаут синтеза (`TTS_TIMEOUT_SECONDS`) |

---

<a id="get-v1voices--каталог-голосов-adr-100"></a>
## GET /v1/voices — каталог голосов ([ADR-100](../../adr/ADR-100-assistant-speech-output.md))

Источник для экрана настроек «голос по умолчанию». Выбранный `id` сохраняется через [`PATCH /v1/preferences`](../preferences/02-api-contracts.md) в поле `defaultVoiceId`.

### Auth
- **JWT-protected**, read-only, без побочных эффектов — как `GET /v1/characters` / `GET /v1/presets`.

### Query-параметры и резолвинг локали
- **`locale` (опц.)** — тот же набор, канонизация и порядок резолва, что у пресетов и персонажей ([ADR-049 §3](../../adr/ADR-049-presets-localization.md)): `?locale=` (вне набора → `422`) → `Accept-Language` → `PRESETS_DEFAULT_LOCALE` → `en`. Третьей переменной под язык каталога не заводится ([TD-035](../../100-known-tech-debt.md)).

### Response (200)
```json
{
  "enabled": true,
  "locale": "ru",
  "defaultVoiceId": "default_female",
  "voices": [
    { "id": "default_male",   "name": "Мужской", "gender": "male" },
    { "id": "default_female", "name": "Женский", "gender": "female" }
  ]
}
```
- **`enabled`** — `VOICE_OUTPUT_ENABLED` этого инстанса. `false` → `voices: []`, `defaultVoiceId: null`; клиент прячет и настройку голоса, и кнопку воспроизведения по этому полю, а не по пустому списку и не по коду ответа.
- **`defaultVoiceId`** — голос, который прозвучит у **этого** пользователя в чате без персонажа: результат резолва `user_preferences.default_voice_id` → `TTS_DEFAULT_VOICE_ID`. Потребитель — предвыбранная строка в списке настроек. `null` только при `enabled: false`.
- **`voices[]`** — только записи реестра с `selectable: true` (на старте две: `default_male`, `default_female`). **Голоса персонажей в каталоге отсутствуют:** они не выбираются пользователем, а их присутствие приглашало бы приложение прислать один из них в настройки.
- **`provider` / `provider_voice_id` / `instructions` наружу не отдаются** — по тому же основанию, что и `persona` персонажа ([ADR-097 §3](../../adr/ADR-097-character-personas.md)): это внутренние параметры, которые правят на слух; отдав их, мы сделали бы правку манеры релизом приложения.
- Порядок элементов = порядок объявления в реестре, един во всех локалях.

**Коды:** `200`; `401`; `422` явный `?locale=` вне набора; `429`. При выключенном флаге — `200 {enabled:false}`, **не `404`** (иначе приложение не отличит «инстанс не умеет» от «бэкенд старее фичи») и **не `503`** (это штатный дефолт, а не мис-конфигурация) — [ADR-097 §7](../../adr/ADR-097-character-personas.md).
