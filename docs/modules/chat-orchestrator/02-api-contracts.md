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
- `sessionId` отсутствует → создаётся новая сессия. На сессию фиксируются: `mode` (billing_mode, credits|byok — **способ оплаты**, [ADR-012](../../adr/ADR-012-assistant-mode-vs-billing-mode.md)), `assistantMode` (тип ассистента chat|code), `model` (опц., см. ниже), `projectId` (опц., см. ниже) и `workspaceProjectId` (привязка к рабочему пространству, [ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).
<a id="model-опц-session-fixed-adr-034"></a>
- **`model` (опц., session-fixed, [ADR-034](../../adr/ADR-034-user-model-selection.md) / [ADR-073](../../adr/ADR-073-dual-credits-llm-providers.md)).** Выбор модели из разрешённого инстансом набора (`GET /v1/models`). Фиксируется на сессию при создании (как `mode`/`assistantMode`/`projectId`); **провайдер чата не меняется на resume**:
  - **без `model`** → сессия создаётся с `chat_sessions.model = NULL` = «дефолтная модель инстанса» (`ANTHROPIC_MODEL`/`OPENAI_MODEL` активного `LLM_PROVIDER`) — обратная совместимость;
  - **с `model`** → должен быть непустой строкой после `strip` (пустая/whitespace → `422`) **и** входить в **chat**-каталог инстанса (`GET /v1/models` с `modality=chat`: без `LLM_PROVIDERS` — allowlist активного провайдера; с dual — union обоих). Fal-id (`modality=photo`/`video`) → **`422 unsupported_model`**. Иначе → **`422 unsupported_model`** (`"model '<x>' is not available on this instance"`). Тихого фолбэка на дефолт нет — явный контракт ([ADR-034 §3](../../adr/ADR-034-user-model-selection.md)).
  - **Resume-сессия:** `model` берётся из сессии (`chat_sessions.model`); поле запроса при resume **игнорируется** (не ошибка) — единообразно с `mode`/`assistantMode`/`projectId`. Смена провайдера внутри чата **не** поддерживается.
  - **Биллинг от выбора модели не зависит** (1 кредит = 1 сообщение, [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md)). Возвращаемый `usage.model` отражает фактически использованную модель.
  - Без `LLM_PROVIDERS` инстанс одно-провайдерный ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) → выбрать чужую (Claude на openai-инстансе) нельзя. Dual ([ADR-073](../../adr/ADR-073-dual-credits-llm-providers.md)) — opt-in.
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
  - **Edge — редактирование ПЕРВОГО сообщения чата ([ADR-040 §4а](../../adr/ADR-040-edit-message-and-regenerate.md)):** усечение удаляет всю историю, сессия становится пустой, но **существует** → `ctx.is_new = False`. Поэтому **workspace-файлы НЕ переинъектируются** (turn-0-only, вариант a [ADR-038 §3.2](../../adr/ADR-038-move-chat-to-workspace.md)) — приемлемо и зафиксировано (симметрия с inline-attachments [ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md)); `instructions` workspace инъектируются как обычно (на каждом ходе, развязано от `is_new`, [ADR-038 §3](../../adr/ADR-038-move-chat-to-workspace.md)). Инлайн-attachments нового хода подаются как turn-0 нового хода. Пересмотр реинъекции файлов → [Q-040-3](../../99-open-questions.md).
  - **Edge — открытый tool-loop ([ADR-040 §4б](../../adr/ADR-040-edit-message-and-regenerate.md)):** редактируемый или последующий ход с pending `tool_calls` / незакрытым барьером ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) — усечение удаляет эти шаги и их `tool_calls` → **никаких осиротевших `tool_calls`/незакрытых барьеров**. «Зависший» tool_call сбрасывается редактированием.
  - **Без `editMessageStepId` `/chat/run` не меняется** (обратная совместимость полная). Миграции нет. В `/chat/tool-result` поле не применимо (редактирование — только новый ход `/chat/run`).
- `attachments[]` (опц., ≤ `ATTACHMENT_MAX_COUNT`, дефолт 10) — **inline base64-вложения** ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), заменяет двухшаговую модель [ADR-014](../../adr/ADR-014-multimodal-attachments.md)). Принимаются **только** в первом (новом) пользовательском message-шаге `/chat/run`; в `/chat/tool-result` — **не** принимаются. Поля вложения:
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
- Size-лимиты: `message` ≤ 32KB, `context` ≤ 64KB (см. [05-security.md](../../05-security.md)). **Тело `/v1/chat/run` и `/v1/chat/v2/run` имеет повышенный transport-лимит** (`ATTACHMENT_REQUEST_BODY_LIMIT`, дефолт 12 MB) для inline base64-вложений — общий лимит `≤512KB` прочих роутов **не меняется**, повышение применяется точным сравнением пути к этим двум роутам (`tool-result` под него не подпадает) ([ADR-020](../../adr/ADR-020-inline-base64-attachments-mvp.md), [05-security.md](../../05-security.md)). Лимиты на вложения: одно ≤ `ATTACHMENT_MAX_BYTES_IMAGE` (дефолт 5 MB) / `ATTACHMENT_MAX_BYTES_DOCUMENT` (дефолт 8 MB), суммарно ≤ `ATTACHMENT_TOTAL_BYTES` (дефолт 10 MB).
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
  "quiz": null
}
```
- **`toolCalls[]` (множественный, [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)) присутствует только при `status=tool_call`** — **ВСЕ** client-side tool-вызовы текущего assistant-хода (parallel tool use), в порядке блоков ответа Claude. Каждый элемент `{ id (доменный UUID = tool_calls.id), name (dot), args }`. **Server-side `site.*` в `toolCalls[]` НЕ попадают** (исполняются на бэке в tool-loop, [ADR-011](../../adr/ADR-011-server-side-tools.md)) — массив несёт только client-side (`files.*`/`calendar.*`/`reminders.*`).
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
- **`quiz` ([ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) — схема ответа общая с `/v1/chat/v2/*`, поэтому поле присутствует и здесь, но на legacy-роуте оно ВСЕГДА `null`.** Квиз порождает инструмент `quiz.generate`, который предлагается модели только при `generationMode=study_learn`, а legacy-путь принудительно использует `general` → инструмент не предлагается, пул не появляется. Поведение legacy `/v1/chat/run` этим полем не меняется (аддитивно, старые клиенты игнорируют). Семантика поля — [§Chat v2 → Response](#quiz-adr-064).
- `usage` присутствует при `assistant_message`/`tool_call`, **а также при `blocked` с `blockReason=max_tokens`** ([ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md)); при policy-blocked (генерация не выполнялась) — отсутствует.
- **`mediaJobs` ([ADR-068](../../adr/ADR-068-media-generate-chat-tools.md)) — аддитивно:** список задач `{ jobId, kind, status, model, creditsCharged }`, поставленных в этом **ходе** tools `media.generate_image` / `media.generate_video`. `null` — задач не было. Клиент опрашивает `GET /v1/media/jobs/{jobId}` (и/или push ADR-067). Биллинг media отдельный от хода чата. Контракт `/v1/media/*` не меняется.
- **`mediaChoices` ([ADR-070](../../adr/ADR-070-media-choices-wizard.md)) — аддитивно, не `quiz`:** пикер параметров media (`selectionId`, `kind`, `step`, `questions[].options` с `value`/`label`/`credits?`). Fal-промпт в ответе **не** отдаётся. Options только из серверного каталога. Клиент тапает как квиз-карточки и шлёт `mediaSelection` на `/v1/chat/v2/run`. `assistantMessage` не глушится. На SSE — только в `done`.

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
Все поля [`POST /v1/chat/run`](#post-v1chatrun) (`userId`, `sessionId`, `projectId`, `message`, `mode`, `assistantMode`, `model`, `workspaceProjectId`, `attachments`, `context`, `editMessageStepId` — семантика, валидация и коды **идентичны**) **плюс одно**:

```json
{ "generationMode": "general | research | reasoning | study_learn" }
```
<a id="generationmode-adr-064"></a>
- **`generationMode` (опц., дефолт `general`, per-turn).** Не фиксируется на сессию: в одном `sessionId` ход может быть `research`, следующий — `general`, затем `study_learn`. Значение вне набора → `422` (`StrictModel`/`Literal`). Отдельной оси «режим диалога» (`dialogMode`) в контракте **нет** — режим один ([ADR-064 §1](../../adr/ADR-064-study-learn-quiz-generation-mode.md)).
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

Тот же body/auth/rate-limit, что у [`POST /v1/chat/v2/run`](#post-v1chatv2run). Ответ: `Content-Type: text/event-stream`.

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
  - `creditCost` — из `chat_generation_credit_cost(mode)` (тот же источник, что у списания).
- **Гейт ОБЪЯВЛЕНИЯ ≠ гейт ПОВЕДЕНИЯ (нормативно).** [`POST /v1/chat/v2/run`](#post-v1chatv2run) принимает `generationMode=study_learn` **на любом инстансе**, независимо от allowlist: приложение, знающее имя режима, работает. Allowlist влияет **только** на состав этого массива. Per-instance флаг **включения** режима (запрос отвергается) — отклонён и не вводится; цена выключателем служить не может (кламп `≤0 → 1`, [§Config](10-generation-modes-implementation.md#config)).
- **`available`** — у **присутствующих** элементов всегда `true`; producer'а, возвращающего `false`, **нет**. Клиент обязан читать гейт как **присутствие/отсутствие элемента**, а не как значение `available` ([ADR-065 §1.8](../../adr/ADR-065-study-learn-advertisement-gate-and-history-spoiler-strip.md)). Поле сохранено для совместимости и зарезервировано под будущее «объявлен, но недоступен» — это будет отдельное решение.
- **Порядок фиксирован и каноничен:** `general`, `research`, `reasoning`, `study_learn` — независимо от порядка перечисления в env; новые режимы добавляются **в конец**, позиции существующих не сдвигаются (клиент вправе рендерить список как есть).
- `reasoningLevel` — серверный effort/budget level для `reasoning` (`low|medium|high`, `CHAT_REASONING_LEVEL`).
- **Forward-compat:** клиент обязан игнорировать неизвестные ему значения `mode` в списке — появление нового режима не является breaking change.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.

---

## Классы tools: client-side vs server-side ([ADR-011](../../adr/ADR-011-server-side-tools.md), [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md))
Три класса инструментов ([ADR-026 §1](../../adr/ADR-026-global-server-side-tools-and-time-now.md)):
- **client-side** (`files.*`, `calendar.*`, `reminders.*`) — исполняет **iOS-клиент**: backend отдаёт `status=tool_call`,
  ждёт `tool_result` через `/v1/chat/tool-result`. Описаны в этом документе.
- **server-side, project-scoped** (`site.*`, website-builder, `SERVER_SIDE_TOOLS`) — исполняет **backend** немедленно в tool-loop, формирует `tool_result` сам
  и продолжает к Anthropic **без** round-trip к iOS; **НЕ** отдаётся клиенту как `status=tool_call`. **Требует проекта.** Схемы и поведение —
  [modules/website-builder/02-api-contracts.md](../website-builder/02-api-contracts.md), [ADR-011](../../adr/ADR-011-server-side-tools.md).
- **server-side, global** (`time.now`, `quiz.generate`, `GLOBAL_SERVER_SIDE_TOOLS`, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)/[ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) — исполняет **backend** немедленно в tool-loop (как `site.*`), но **НЕ требует проекта**. В `toolCalls[]` наружу **НЕ** попадают. Контракты — [§`time.now`](#timenow--server-side-global-tool-adr-026) и [§`quiz.generate`](#quizgenerate--server-side-global-tool-режимный-adr-064) ниже.
  - **Предложение модели внутри класса различается (не переносить по аналогии!):** `time.now` предлагается **ВСЕГДА** (utility, [ADR-026 §3](../../adr/ADR-026-global-server-side-tools-and-time-now.md)); `quiz.generate` — **только** когда эффективный режим хода = `study_learn` (ось C, [ADR-064 §3](../../adr/ADR-064-study-learn-quiz-generation-mode.md)). «Global» означает «без проекта», а не «без гейта».
- Orchestrator различает класс по доменному имени (статические реестры `SERVER_SIDE_TOOLS = {site.*}`, `GLOBAL_SERVER_SIDE_TOOLS = {time.now, quiz.generate, media.generate_image, media.generate_video, media.ask_params}`, непересекающиеся). Дополнительный реестр `TOOL_GENERATION_MODES` (`quiz.generate → {study_learn}`) задаёт ось C; инструменты вне этого реестра по режиму не гейтятся. domain↔anthropic
  mapping (точка→подчёркивание) расширяется server-side именами (`site.write_file ↔ site_write_file`, `time.now ↔ time_now`, …). Guard на число
  server-side раундов — `MAX_SERVER_TOOL_ROUNDS` (дефолт 16) — общий для project-scoped и global server-side раундов.
- **Гейтинг по наличию проекта ([ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md)):** `site.*` (`SERVER_SIDE_TOOLS`) предлагаются Claude **только** когда у сессии есть `project_id` (создана с `projectId`). В «чистом чате» (`chat_sessions.project_id IS NULL`) `site.*` в tool-набор **не включаются** — Claude их не видит и не вызывает. **`time.now` (`GLOBAL_SERVER_SIDE_TOOLS`) под этот гейт НЕ подпадает** — предлагается всегда ([ADR-026 §3](../../adr/ADR-026-global-server-side-tools-and-time-now.md)). См. [03-architecture.md §Гейтинг tools](03-architecture.md#гейтинг-site-tools-по-наличию-проекта-adr-022).
- **Гейтинг по режиму генерации (ось C, [ADR-064 §3](../../adr/ADR-064-study-learn-quiz-generation-mode.md)):** инструмент из `TOOL_GENERATION_MODES` предлагается **только** в перечисленных режимах. Гейт считается по **эффективному** режиму хода — тому же значению, которое уходит провайдеру и в биллинг; legacy-путь принудительно использует `general`, поэтому mode-gated инструменты на `/v1/chat/run` не предлагаются **по построению** (без отдельной ветки-исключения). Оси A/B/C складываются по И — таблица «инструмент × оси» в [03-architecture.md §Оси гейтинга tool-набора](03-architecture.md#оси-гейтинга-tool-набора-adr-022--adr-026--adr-064).

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

### Общие правила схем
- Все схемы — Pydantic v2, `extra='forbid'`.
- Даты — ISO8601 (RFC3339), UTC или с offset. **Исключение:** календарные `start`/`end` (`calendar.read`, `calendar.create_events`) — ISO8601-datetime в локальном времени **без** offset (naive local), см. раздел «Контракт календарных инструментов: `start`/`end`» ниже и [ADR-027](../../adr/ADR-027-calendar-read-contract-alignment.md).
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

### blockReason enum (повтор для удобства)
`trial_used | subscription_required | subscription_expired | credits_empty | byok_disabled | byok_invalid | rate_limited | policy_denied | max_tokens` (источник — [ADR-004](../../adr/ADR-004-blocked-http-200.md); `max_tokens` добавлен [ADR-025](../../adr/ADR-025-parallel-tool-calls-and-max-tokens-truncation.md) — обрезка ответа по лимиту output-токенов, в отличие от прочих policy-причин срабатывает **после** начала генерации: `usage`/`messageStepId`/`stepId` присутствуют, кредит не списывается).

---

## GET /v1/tools — каталог инструментов ([ADR-019](../../adr/ADR-019-tools-catalog-endpoint.md))
Машиночитаемый каталог всех поддерживаемых backend tools (**17**, включая `time.now` [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md), `quiz.generate` [ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md), media generate/ask_params [ADR-068](../../adr/ADR-068-media-generate-chat-tools.md)/[ADR-070](../../adr/ADR-070-media-choices-wizard.md)). Источник — `src/app/chat/tools.py` (single source of truth: `_ARGS_BY_TOOL`, `MUTATING_TOOLS`, `SERVER_SIDE_TOOLS`, `GLOBAL_SERVER_SIDE_TOOLS`, `TOOL_GENERATION_MODES`, `anthropic_tool_definitions()`). Эндпоинт **не** параметризуется ни `assistantMode`, ни наличием проекта, ни `generationMode` — возвращает полный технический реестр backend (включая `site.*`, `time.now` и `quiz.generate`). Runtime-фильтрация tool-набора, предлагаемого модели (ось A — `project_id`, [ADR-022](../../adr/ADR-022-optional-project-and-tool-gating.md); ось B — `assistantMode`, [Q-012-1](../../99-open-questions.md); ось C — режим генерации, [ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)), — concern tool-loop'а, а не каталога. Поэтому `quiz.generate` присутствует в каталоге **всегда**, хотя предлагается модели только в режиме `study_learn`.

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
      "inputSchema": { "type": "object", "properties": { "path": { "type": "string" } }, "required": ["path"] }
    },
    {
      "name": "site.write_file",
      "description": "Write or overwrite a file in the website project...",
      "mutating": true,
      "execution": "server",
      "inputSchema": { "type": "object", "properties": { "...": {} } }
    }
  ]
}
```
- `name` — **доменное** имя с точкой (как в публичном iOS-контракте), НЕ anthropic-underscore (`files_read` — деталь Anthropic-транспорта, BUG-3).
- `description` — из `descriptions` в `anthropic_tool_definitions()`.
- `mutating` — `name ∈ MUTATING_TOOLS` (требует audit при исполнении).
- `execution` — `"server"` если `name ∈ SERVER_SIDE_TOOLS ∪ GLOBAL_SERVER_SIDE_TOOLS` (`site.*` — [ADR-011](../../adr/ADR-011-server-side-tools.md); `time.now` — [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md); исполняет backend); иначе `"client"` (исполняет iOS).
<a id="inputschema--нормативный-формат"></a>
- **`inputSchema` — JSON Schema аргументов инструмента (нормативно).** Строится из `model_json_schema()` модели args, из которой **вырезана модельная метаинформация**: корневые `title` (= имя Python-класса) и `description` (= docstring класса), а у инструментов с self-contained-схемой ([§`quiz.generate`](#quizgenerate--server-side-global-tool-режимный-adr-064)) — те же два ключа и у **инлайненных** определений вложенных моделей. **Сохраняются:** пофилдовые `title`/`description` (из `Field(...)`), `type`/`properties`/`items`/`required`/`enum`/`additionalProperties` и ограничивающие ключи (`minItems`/`maxItems`/`maxLength`/…). Формат **не** определяется как «сырой вывод `model_json_schema()`»: равенство сырому выводу нарушало бы инвариант ниже.
  - **Инвариант «внутренние идентификаторы не покидают процесс» (нормативно, шире этого поля).** Ни один артефакт, уходящий наружу — `inputSchema` в `GET /v1/tools`, `description` записи каталога (`TOOL_DESCRIPTIONS`), `input_schema`/`parameters` и `description`, уходящие **провайдеру** — не должен содержать внутренних идентификаторов разработки: ссылок `ADR-NNN`/`TD-NNN`/`Q-NNN-N`/`BUG-N`, имён внутренних классов (`*Args`, `GlobalToolHandlers`, `SiteToolHandlers`), имён внутренних констант/реестров (`MAX_SERVER_TOOL_ROUNDS`, `_ARGS_BY_TOOL`). Это та же норма, что [08-api-documentation.md §R2ter](../../08-api-documentation.md) предъявляет к user-facing текстам OpenAPI, распространённая на **вторую** поверхность утечки — tool-контракт: docstring внутренней модели, попавший в схему, уезжает и клиенту, и в промпт модели.
  - **Способ соблюдения — вырезание на границе, а не дисциплина docstring'ов.** Требование адресовано **генератору схемы** (одна точка, `tool_input_schema`), а не авторам моделей: правило «не писать ADR-ссылок в docstring» не проверяемо и ломается первым же новым инструментом. Docstring'и внутренних моделей остаются нормальной внутренней документацией.
  - **Покрытие — тест-детектор, а не ревью глазами:** скан **всех** записей каталога и **всех** определений, уходящих провайдеру, регуляркой на перечисленные классы идентификаторов → ноль совпадений; плюс проверка, что пофилдовые описания при этом **не** пусты (вырезание не должно выкосить полезную часть). См. [09-testing.md](09-testing.md#unit--каталог-инструментов-и-утечка-внутренних-идентификаторов).
- Порядок — детерминированный (по `_ARGS_BY_TOOL`).

### Полный список (15)
| name | execution | mutating |
|---|---|---|
| files.read | client | нет |
| files.write | client | **да** |
| files.list | client | нет |
| files.mkdir | client | **да** |
| calendar.read | client | нет |
| calendar.create_events | client | **да** |
| reminders.read | client | нет |
| reminders.create | client | **да** |
| site.write_file | **server** | **да** |
| site.preview | **server** | нет |
| site.list | **server** | нет |
| site.read | **server** | нет |
| site.delete | **server** | **да** |
| time.now | **server** (global, [ADR-026](../../adr/ADR-026-global-server-side-tools-and-time-now.md)) | нет |
| quiz.generate | **server** (global, режимный, [ADR-064](../../adr/ADR-064-study-learn-quiz-generation-mode.md)) | нет |

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
    { "id": "gpt-4o", "displayName": "GPT-4o", "name": "GPT-4o", "default": true, "provider": "openai", "modality": "chat", "variant": null, "family": null },
    { "id": "claude-sonnet-4-5", "displayName": "Claude Sonnet 4.5", "name": "Claude Sonnet 4.5", "default": false, "provider": "anthropic", "modality": "chat", "variant": null, "family": null },
    { "id": "fal-ai/nano-banana-pro", "displayName": "Nano Banana Pro", "name": "Nano Banana Pro", "default": true, "provider": "fal", "modality": "photo", "variant": "Text to Image", "family": "Nano-Banana-Pro" }
  ]
}
```
- `id` — для `modality=chat` уходит в `POST /v1/chat/run` `model`. Для photo/video — endpoint fal; в `chat.model` **не** принимается (`422`).
- `displayName` / `name` — одно и то же человекочитаемое имя (`name` — дубль для клиентов, которые читают `name`).
- `default` (bool) — у **chat** ровно один `true` (дефолт инстанса), он **первый** в массиве. При включённом fal у photo свой дефолт (`fal-ai/nano-banana-pro`). Video — все `false`.
- `provider` (`openai`\|`anthropic`\|`fal`) — аддитивное поле. Старые клиенты игнорируют неизвестные ключи.
- `modality` (`chat`\|`photo`\|`video`) — селектор чата берёт только `chat`.
- `variant` / `family` — режим и семейство fal; у chat всегда `null`.
- **Пустой chat-allowlist** ⇒ дефолт инстанса первым + встроенный продуктовый каталог провайдера ([ADR-076](../../adr/ADR-076-builtin-chat-product-catalog.md)). Env allowlist добавляет extras и может переименовать; встроенные id не прячет.
- Смена провайдера внутри чата **не** поддерживается (resume игнорирует `model`). `GET /v1/media/models` (короткие id, `modes[]`, цены) не заменяется.

**Коды:** `200`; `401` нет/невалидный JWT; `429` rate-limit.

## GET /v1/presets — пресеты промтов ([ADR-035](../../adr/ADR-035-prompt-presets-endpoint.md))

Источник для чипов-пресетов на главном экране чата iOS (экран 4). Тап по чипу подставляет `prompt` в композер. Набор и тексты меняются деплоем backend **без релиза iOS-приложения**. Провайдер/инстанс-агностично: идентичный ответ на всех действующих инстансах. Источник — статический реестр в коде (`src/app/chat/presets.py`, single source of truth, по образцу [`GET /v1/tools`](#get-v1tools--каталог-инструментов-adr-019)).

### Auth
- **JWT-protected** (как `GET /v1/tools`/`GET /v1/models`): `Authorization: Bearer <JWT>` обязателен. Каталог не секретен, контур авторизации единый. Per-user rate-limit как у прочих read-эндпоинтов (`enforce_other_limits`). Метод `GET` (read-only, без побочных эффектов: не создаёт сессию, не пишет ledger/audit).

### Query-параметры (локализация, [ADR-049](../../adr/ADR-049-presets-localization.md))
- **`locale` (опц., str).** Явный выбор локали каталога. Допустимый набор — поддерживаемые локали (`en`, `ru`; расширяется). Нормализуется `strip().lower()`. **Явно указанное значение вне набора → `422`** (`"locale '<x>' is not supported"`) — симметрично строгому `422 unsupported_model` ([ADR-034 §3](#model-опц-session-fixed-adr-034)); молчаливой подмены явного запроса нет.

### Резолвинг локали (порядок, [ADR-049 §3](../../adr/ADR-049-presets-localization.md))
Первый сработавший шаг выигрывает:
1. **Query `?locale=`** — валидное значение из набора; невалидное → `422` (см. выше).
2. **`Accept-Language`** (если query нет) — первый **поддерживаемый** primary-subtag: значение делится по `,`, отбрасывается `;q=...`, берётся часть до `-` в lower (`ru-RU`→`ru`, `en-US`→`en`); первый subtag из набора — результат. Ни одного поддерживаемого / пусто / нераспознано → **тихий fallback** к шагу 3 (заголовок не строго клиент-контролируем → без `422`).
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
      "prompt": "Помоги спланировать предстоящую неделю. Расспроси меня о приоритетах, сроках и обязательствах, а затем предложи сбалансированное расписание по дням."
    }
  ]
}
```
- `locale` ([ADR-049 §5](../../adr/ADR-049-presets-localization.md), **аддитивно**) — фактически отданная локаль (из поддерживаемого набора; результат резолвинга). Старые клиенты игнорируют поле.
- `id` — стабильный slug (`[a-z0-9_]`, snake_case), уникален в наборе; стабилен между релизами. **Не локализуется** (общий для всех локалей).
- `title` — отображаемое имя чипа (на выбранной локали).
- `icon` — имя **SF Symbol** (например `calendar`, `doc.text`, `camera`); клиент рендерит `Image(systemName:)`, при отсутствии символа — клиентский fallback. Не emoji ([ADR-035 §4](../../adr/ADR-035-prompt-presets-endpoint.md)). **Не локализуется** (стабильный ресурс iOS).
- `prompt` — plain-text (на выбранной локали), подставляется в композер при тапе (без шаблонов/плейсхолдеров на старте).
- Порядок элементов = порядок чипов на экране (детерминированный, порядок объявления в реестре) — **един во всех локалях**. Все 4 поля пресета обязательны и непусты.
- **Per-field EN-fallback:** если у выбранной локали не заполнено какое-то поле пресета, оно берётся из EN (канон); незаполненная/неизвестная локаль целиком → EN-каталог.

**Дефолтный набор (7, со скрина):** `plan_week`, `meeting_notes`, `tasks_from_photo`, `design_brief`, `daily_review`, `summarize_text`, `project_structure` — EN-тексты в [ADR-035 §3](../../adr/ADR-035-prompt-presets-endpoint.md), RU-тексты в [ADR-049 §1.1](../../adr/ADR-049-presets-localization.md).

**Совместимость:** без env и без запроса локали (`locale` отсутствует, `Accept-Language` без поддерживаемых, дефолт `en`) → EN-ответ как раньше; поле `locale` при этом = `"en"`. Без миграции; провайдер-агностично ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)).

**Коды:** `200`; `401` нет/невалидный JWT; `422` явный `?locale=` вне набора; `429` rate-limit.
