# 08 — API Documentation (OpenAPI / Swagger)

Сквозной стандарт оформления автогенерируемой OpenAPI-документации FastAPI (`/docs`, `/redoc`, `/openapi.json`). Цель — документация **на русском языке**, читаемая как нормальное руководство по API, с рабочей авторизацией в Swagger UI и явно описанными бизнес-блокировками.

Это convention поверх уже реализованного backend (все endpoint — см. [modules/api-gateway/02-api-contracts.md](modules/api-gateway/02-api-contracts.md)). Бизнес-контракты не меняются — меняется только их представление в OpenAPI. Реализация — `backend` в `src/app/` (app factory `create_app()`, схемы `src/app/schemas/`, роутеры `src/app/api_gateway/routers/`).

> Это не ADR: оформление документации не является значимым архитектурным решением (не меняет границы компонентов, контракты, модель данных, безопасность по существу). Единственный аспект с эффектом на поверхность атаки — отключение `/docs` в prod — зафиксирован здесь и в [05-security.md](05-security.md), отдельный ADR не требуется.

## R1. Язык

| Что | Язык |
|---|---|
| `FastAPI(description=...)` — общее описание API | русский |
| `summary` каждого endpoint | русский |
| `description` каждого endpoint | русский |
| `description` тегов (`openapi_tags`) | русский |
| Описания полей схем (`Field(description=...)`) | русский |
| Описания response-моделей и примеров | русский |
| Сообщения об ошибках валидации (`detail`) | как есть (генерит FastAPI/Pydantic) |

Остаются **в оригинале** (не переводятся): имена endpoint-путей (`/v1/chat/run`), имена полей схем (`sessionId`, `blockReason`), enum-значения (`assistant_message`, `credits`, `trial_used`), коды ошибок (`validation_error`, `unauthorized`), имена tools (`files.write`), HTTP-методы и коды, имена заголовков (`Authorization`, `X-Device-Id`, `X-Request-Id`).

Правило: **описываем по-русски, идентифицируем по-английски**. Описание поля `sessionId` — на русском («Идентификатор сессии…»), само имя поля — `sessionId`.

## R2. Security scheme (JWT Bearer)

- Объявить в OpenAPI HTTP Bearer scheme c `bearerFormat: JWT` (механизм FastAPI — `fastapi.security.HTTPBearer`, имя scheme в схеме — `bearerAuth` или эквивалент по умолчанию).
- Все `/v1/*` endpoint помечены как требующие этот scheme → в Swagger UI у них значок замка, и работает кнопка **Authorize** (вводится `Bearer <JWT>` один раз, применяется ко всем защищённым вызовам).
- Auth-модель — JWT **RS256**, claim `sub` = userId; см. [05-security.md](05-security.md). В `description` security scheme на русском кратко: «JWT (RS256). В claim `sub` — userId; `userId` в теле запроса обязан совпадать с `sub`, иначе `403`».
- Публичные служебные endpoint **без** auth-требования в OpenAPI: `GET /health`, `GET /healthz` (алиас `/health`, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)), `GET /ready`, `GET /metrics` (последний защищён сетью/scrape-токеном, не JWT — см. [07-deployment.md](07-deployment.md#health--readiness)). У них **не** должно быть значка замка.
- Объявление security scheme в OpenAPI **не подменяет** реальную проверку JWT (она в `app/api_gateway/auth.py` / `deps.py`) — это только описание для клиента и Swagger UI. Источник истины аутентификации не меняется.

## R3. Бизнес-блокировки (status=blocked, HTTP 200)

Документация обязана явно объяснить нестандартное правило [ADR-004](adr/ADR-004-blocked-http-200.md): бизнес-блокировка — это **успешный** ответ `200 OK` с телом `{status:"blocked", blockReason}`, а не 4xx.

- В `description` endpoint `/v1/chat/run` и `/v1/chat/tool-result` — абзац на русском: «Блокировка по бизнес-правилам возвращается с HTTP 200 и полем `blockReason` (машиночитаемо). Технические ошибки — 4xx/5xx (см. таблицу кодов)».
- В общем `description` API (R6) — короткая ссылка на это правило, чтобы интегратор не искал 4xx там, где приходит 200.
- Поле `blockReason` (в `ChatResponse` и в `reasons[]` `/policy/effective`) описать как enum с расшифровкой **каждого** из 8 значений: что означает и что должен сделать UI. Канонический источник значений — [ADR-004](adr/ADR-004-blocked-http-200.md); документация не вводит новых значений.

### Расшифровка blockReason (для описаний полей и общего раздела)

| Значение | Что означает | Что делает UI |
|---|---|---|
| `trial_used` | Бесплатная пробная генерация уже использована, подписки нет. | Предложить оформить подписку. |
| `subscription_required` | Действие требует активной подписки, её нет. | Экран оформления подписки. |
| `subscription_expired` | Подписка была, но истекла/отозвана. | Предложить продлить подписку. |
| `credits_empty` | Баланс кредитов исчерпан (режим `credits`). | Показать баланс, предложить пополнение/подписку. |
| `byok_disabled` | Режим `byok` выбран, но BYOK выключен пользователем. | Включить BYOK в настройках. |
| `byok_invalid` | Ключ BYOK отсутствует или невалиден (`keyStatus=invalid`, либо ключ не задан при `mode=byok` и активной подписке — `byok=missing`). | Добавить/исправить ключ в настройках. |
| `rate_limited` | Транспортное превышение rate limit; всегда возвращается как HTTP `429` (gateway-concern). НЕ приходит как `status=blocked` body и НЕ входит в `/policy/effective.reasons[]`. Значение enum сохранено только для HTTP-слоя (см. [ADR-004](adr/ADR-004-blocked-http-200.md), BLK-7b в [09-e2e-testing.md](09-e2e-testing.md)). | Показать «слишком часто», предложить повторить позже. |
| `policy_denied` | Общий fallback для непредвиденного состояния Policy Engine. | Generic-сообщение «недоступно», лог/ретрай. |

### Дискриминация ответа `/chat/run` и `/chat/tool-result`

Ответ — одна модель `ChatResponse` (`src/app/schemas/chat.py`) с тремя взаимоисключающими состояниями по полю `status`. Документация обязана сделать варианты очевидными:
- `status=assistant_message`: присутствуют `assistantMessage`, `usage`; нет `toolCall`, `blockReason`.
- `status=tool_call`: присутствуют `toolCall`, `usage`; нет `assistantMessage`, `blockReason`.
- `status=blocked`: присутствует `blockReason`; нет `assistantMessage`, `toolCall`, `usage`.

Способ (на усмотрение `backend`, любой даёт читаемый результат): либо описать инвариант в `description` модели + три именованных примера `openapi_examples` (`assistant_message`, `tool_call`, `blocked`), либо ввести discriminated-union response с тремя под-моделями. **Обязательный минимум** — три именованных примера ответа на каждый из двух chat-endpoint. Менять wire-формат (имена/типы полей) запрещено — это сломает контракт.

## R4. Теги и группировка

Сгруппировать endpoint по модулям через `tags` + объявить порядок и русские описания в `openapi_tags`. Порядок тегов = порядок пользовательского сценария.

| Тег | Endpoint | Описание тега (русский, кратко) |
|---|---|---|
| `Chat` | `POST /v1/chat/run`, `POST /v1/chat/tool-result` | Диалог с ассистентом и tool-loop (вызовы инструментов на устройстве). |
| `Policy` | `GET /v1/policy/effective` | Эффективные права пользователя для UI (можно ли генерировать и почему нет). |
| `Wallet` | `GET /v1/wallet`, `POST /v1/wallet/consume` | Баланс кредитов и списание (1 кредит = 1 сообщение). |
| `Subscription` | `POST /v1/subscription/sync` | Синхронизация подписки StoreKit и начисление кредитов периода. |
| `BYOK` | `POST /v1/byok/set`, `POST /v1/byok/toggle`, `POST /v1/byok/delete` | Свой ключ Anthropic (Bring Your Own Key): сохранение, включение, удаление. |
| `Health` | `GET /health`, `GET /healthz`, `GET /ready`, `GET /metrics` | Служебные проверки и метрики (без JWT). `/healthz` — алиас `/health` ([ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)). |

Каждый endpoint имеет ровно один тег из таблицы. Порядок в `openapi_tags`: `Chat`, `Policy`, `Wallet`, `Subscription`, `BYOK`, `Health`.

## R5. Примеры (request/response)

Для ключевых endpoint — осмысленные примеры на русском (значения-плейсхолдеры реалистичны: UUID-подобные id, осмысленный текст сообщения по-русски). Минимум:

| Endpoint | Обязательные примеры |
|---|---|
| `POST /v1/chat/run` | request (`mode=credits`); response `assistant_message`; response `tool_call` (например, `files.read`); response `blocked` (`credits_empty`). |
| `POST /v1/chat/tool-result` | request с `result` (продолжение tool-loop); request с `error`; response `assistant_message` (финал loop). |
| `POST /v1/wallet/consume` | request (`amount=1`, `requestId`); response (`newBalance`, `ledgerTxId`). |
| `POST /v1/byok/set` | request (`apiKey` — плейсхолдер, помечен «не логируется»); response `keyStatus=valid` и `keyStatus=invalid`. |

Tool-loop сценарий описать связно (в `description` тега `Chat` или endpoint `/chat/run`): `run` → `tool_call` → клиент исполняет tool → `tool-result` → `assistant_message`. Использовать согласованные id между примерами `tool_call.id` и `tool-result.toolCallId`, чтобы сценарий читался end-to-end.

Запрещено в примерах: реальные секреты, реальные JWT, реальные ключи Anthropic, реальные StoreKit payload. Только очевидные плейсхолдеры. Для `apiKey`, `transaction`, `Authorization` — плейсхолдер + пометка о redaction (R7 [05-security.md](05-security.md)).

## R6. Метаданные API

В `create_app()` (`src/app/main.py`) при создании `FastAPI(...)` задать:

| Параметр | Значение |
|---|---|
| `title` | `claude-ios-backend` (без изменений). |
| `version` | текущая версия приложения (на момент фичи `0.1.0`; источник версии не меняется). |
| `description` | русский multiline-текст: назначение сервиса (backend-оркестратор Claude для iOS-приложения), кратко бизнес-правила доступа (trial → подписка/кредиты → BYOK), правило blocked=HTTP 200 (R3) с отсылкой к перечню `blockReason`, требование JWT (R2). Без раскрытия секретов и внутренних деталей реализации. |
| `contact` / `license` / `terms_of_service` | опционально, на усмотрение `backend`. Если заданы — без выдуманных URL/email; иначе не задавать. |

`description` должен дать интегратору контекст за один экран: что это, как авторизоваться, как читать `blocked`.

## R7. Доступность /docs в prod (env-флаг)

Документационные endpoint должны отключаться в production.

- Новая env-переменная **`DOCS_ENABLED`** (bool). Дефолт — `true` (удобно для dev/CI/staging).
- При `DOCS_ENABLED=false`: `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` — `/docs`, `/redoc`, `/openapi.json` возвращают `404`.
- В prod значение задаётся через секрет-менеджер/env согласно [07-deployment.md](07-deployment.md#конфигурация-env). Рекомендация prod — `false` (схему API не раскрывать публично).
- Флаг добавляется в `Settings` (`src/app/config.py`, alias `DOCS_ENABLED`) рядом с прочими тумблерами; `create_app()` читает его при инициализации.
- Поведение зафиксировано как security-мера (снижение раскрытия API surface) — см. [05-security.md](05-security.md#документация-api-в-prod).

## R8. Читаемость (acceptance)

Документация считается «удобной для чтения», если:
- Каждый endpoint имеет короткий `summary` (≤ ~60 символов, повелительно/назывательно: «Запустить шаг диалога», «Списать кредиты») и содержательный `description` (что делает, когда вызывать, что вернёт, нюансы — blocked/идемпотентность/redaction).
- Endpoint сгруппированы тегами в порядке сценария (R4); внутри Swagger UI читаются как разделы руководства.
- У защищённых endpoint виден замок и работает Authorize; у `Health` — нет.
- `blockReason` раскрыт (R3): интегратор понимает каждое значение без чтения исходников.
- В ключевых endpoint есть примеры request/response (R5), включая tool-loop и blocked.
- `description` API (R6) даёт стартовый контекст.

## Scope / Out-of-scope

**В scope:** правки `src/app/main.py` (метаданные, security scheme, теги, docs-флаг), `src/app/config.py` (`DOCS_ENABLED`), `src/app/schemas/*.py` (русские `description` полей, примеры, описание blocked-вариантов), `src/app/api_gateway/routers/*.py` (русские `summary`/`description`/`tags`/`responses`/`examples` на декораторах роутов), `src/app/api_gateway/routers/health.py` (исключение служебных из security).

**Out-of-scope:** изменение wire-формата (имена/типы полей, пути, методы, коды) — запрещено, иначе ломается контракт [modules/api-gateway/02-api-contracts.md](modules/api-gateway/02-api-contracts.md); изменение бизнес-логики, auth, rate limit; новые endpoint; перевод технических идентификаторов.

## Открытые вопросы
Нет. Все решения зафиксированы выше; дефолты явны.
