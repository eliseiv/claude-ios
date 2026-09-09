# 06 — Testing Strategy

## Пирамида
| Уровень | Доля | Что покрывает | Инструменты |
|---|---|---|---|
| Unit | ~60% | Чистая логика: Policy Engine (state machine), биллинг-правило (1 кредит = 1 сообщение / 1 списание на message-шаг, ADR-006), валидация tool-схем, encryption helpers. | pytest, без I/O |
| Integration | ~30% | Endpoint + реальные PostgreSQL/Redis (testcontainers), миграции, идемпотентность, атомарность ledger. Внешние HTTP (Anthropic/Apple) — мок через respx. | pytest-asyncio, testcontainers, respx |
| E2E | ~10% | Полные сценарии: trial-once, blocked при истёкшей подписке, tool-loop в несколько шагов, BYOK routing. | pytest против поднятого app + контейнеры |

## Coverage gate
- Глобальный минимум: **80%** (`--cov-fail-under=80`, см. [02-tech-stack.md](02-tech-stack.md)).
- Критические пакеты (`policy`, `wallet`, `byok`) — целевое покрытие **≥ 95%**, проверяется per-package в CI.

## Обязательные тест-кейсы (привязка к AC из 00-vision)
| Тест | AC | Уровень |
|---|---|---|
| Trial доступен ровно 1 раз, второй → `trial_used` | AC-1 | integration |
| `/chat/run` blocked при `subscription=expired`, mode=credits и mode=byok | AC-2 | integration |
| Конкурентные `consume` с одним idempotency key (для chat-debit — один `messageStepId`) → одно списание | AC-3 | integration |
| Re-entry message-шага (`/chat/run` + N×`/chat/tool-result`) → ровно один debit по `messageStepId` | AC-3, AC-4 | e2e |
| `consume` при balance < amount → отказ, баланс не отрицателен | AC-3 | unit+integration |
| Tool-loop: run → tool_call → tool-result → tool_call → ... → assistant_message | AC-4 | e2e |
| Повторный tool-result с тем же `toolCallId` → идемпотентно | AC-4 | integration |
| BYOK ключ зашифрован в БД; логи не содержат plaintext | AC-5 | integration |
| `/policy/effective` совпадает с фактическим решением `/chat/run` для всех состояний | AC-6 | integration |
| Audit-запись на каждое мутирующее tool-действие и каждое списание | AC-7 | integration |

## Тест-кейсы мультимодальных вложений (ADR-020)
| Тест | Уровень |
|---|---|
| `image` (jpeg/png/gif/webp) → корректный Anthropic `image`-блок (base64, media_type из записи) | unit |
| `document` (PDF) на Anthropic → нативный `document`-блок base64; текст НЕ извлекается | unit |
| `document` (PDF) на OpenAI (`LLM_PROVIDER=openai`) → **НЕ `422`** ([ADR-041](adr/ADR-041-openai-native-pdf-attachment.md), закрывает [TD-023](100-known-tech-debt.md)): content-часть `file` (data-URI) ИЛИ извлечённый `pypdf`-текст как text-блок (фолбэк); turn-0 сборка/плейсхолдеры/персист без изменений (сырой base64 не персистится) | unit |
| `text` (plain/markdown/csv/json) → `text`-блок с разметкой имени файла; невалидный UTF-8 → `422` | unit |
| MIME вне allowlist (DOCX/HEIC/zip/octet-stream) → `422 unsupported_media_type` | unit+integration |
| Рассогласование `type`/`mediaType` ↔ magic bytes (бинарь под видом image/png) → `422` | unit |
| Невалидный/обрезанный base64 → `422` (не 500) | unit |
| Лимит размера одного вложения / суммарного / числа — проверка ДО декодирования → `413`/`422` | unit+integration |
| Повышенный body-лимит применяется **только** к upload-роутам `/v1/chat/run` (ADR-020) и `POST /v1/workspaces/{id}/files` (ADR-045); прочие роуты (включая CRUD `/v1/workspaces/{id}` и `DELETE …/files/{file_id}`) сохраняют `≤512KB` | integration |
| Workspace upload (ADR-045): файл ровно 8 MB (`WORKSPACE_FILE_MAX_BYTES`) в base64 проходит gateway (не 413 на транспорте); тело > `WORKSPACE_REQUEST_BODY_LIMIT` → `413`; инвариант `WORKSPACE_REQUEST_BODY_LIMIT ≥ WORKSPACE_FILE_MAX_BYTES*4/3 + JSON-запас` | unit+integration |
| PDF page-guard: PDF с числом страниц > `ATTACHMENT_PDF_MAX_PAGES` → `422` (анти-bomb) | unit |
| URL-вложение / `source.type=url` → отвергается (нет backend-fetch, анти-SSRF) | unit |
| Реплей: `chat_steps.payload` user-turn содержит плейсхолдер, НЕ base64; на витке ≥1 tool-loop тяжёлый контент не реплеится | integration |
| Биллинг: сообщение с вложениями = 1 кредит (mode=credits и mode=byok); usage пишется в meta | integration |
| Логи/audit не содержат `attachments[].data` и декодированного содержимого (redaction) | integration |
| Вложения в `/chat/run` принимаются; `/chat/tool-result` их не принимает (`extra='forbid'`) | unit |
| **E2E (реальный Anthropic):** image + PDF + text в одном сообщении → корректный assistant_message; подтверждает wire-совместимость `document`-блока на SDK 0.39.0 ([TD-016](100-known-tech-debt.md)). **Статус: обязателен, но пока НЕ выполнен — org Anthropic отключена (generation blocked); прогон обязателен сразу после восстановления org. До прогона live-совместимость PDF `document`-блока остаётся неподтверждённой (TD-016 открыт).** | e2e (`@pytest.mark.external`) |

## Тест-кейсы инструмента `time.now` ([ADR-026](adr/ADR-026-global-server-side-tools-and-time-now.md))

**Контракт Clock для qa (детерминизм).** `time.now` берёт время через инъектируемый `Clock` (Protocol с `now() -> datetime` timezone-aware UTC; дефолт `SystemClock`). Тесты подают `FixedClock(fixed_dt)` в `GlobalToolHandlers` → результат полностью детерминирован; **прямой `datetime.now()` в коде `time.now` запрещён** (иначе тест недетерминирован). qa проверяет точный JSON-шейп при фиксированном `fixed_dt`.

| Тест | Уровень |
|---|---|
| Без `tz`: result = `{utc, unix, weekday}` (ISO8601 `+00:00`, целочисленный unix, верный день недели по `fixed_dt`); полей `local`/`timezone` НЕТ | unit |
| С валидным `tz` (`Europe/Moscow`): дополнительно `local` (ISO8601 с offset зоны) + `timezone` (нормализованное имя); `utc`/`unix`/`weekday` соответствуют `fixed_dt` | unit |
| Невалидный/неизвестный `tz` (`Mars/Phobos`, мусор) → `ToolExecution.error(code="invalid_timezone")`, НЕ исключение/падение хода; ход продолжается | unit |
| `tz` длиннее лимита (`> 64`, [Q-026-1](99-open-questions.md)) → `invalid_timezone` (до резолва `zoneinfo`) | unit |
| Args `extra=forbid`: лишний ключ в args → ошибка валидации (как у прочих tools) | unit |
| Детерминизм: `FixedClock` → одинаковый результат при повторных вызовах; `SystemClock` (дефолт) даёт текущее время | unit |
| Маршрутизация global server-side: `time.now` исполняется в tool-loop **без проекта** (`project_id IS NULL`) — `_external_project_id` НЕ вызывается, `assert external_project_id is not None` не срабатывает; в `toolCalls[]` наружу НЕ попадает; loop продолжается к Anthropic | integration |
| `anthropic_tool_definitions(include_server_side=False)` (нет проекта) содержит `time.now`, НЕ содержит `site.*`; `GET /v1/tools` отдаёт **весь** реестр (ассерт — равенство МНОЖЕСТВА имён каталога независимо объявленному `ALL_TOOL_NAMES`, не сравнение длины с `_ARGS_BY_TOOL`, которое тавтологично, и не литерал-число; нормативный состав — [chat-orchestrator/02-api-contracts §GET /v1/tools](modules/chat-orchestrator/02-api-contracts.md#get-v1tools--каталог-инструментов-adr-019)), `time.now`: `execution=server`, `mutating=false` | unit+integration |
| `time.now` предлагается во **всех** режимах генерации (ось C его не гейтит) — в отличие от `quiz.generate`, доступного только при `study_learn` ([modules/chat-orchestrator/09-testing.md §Study & Learn](modules/chat-orchestrator/09-testing.md#integration--study--learn-квиз-adr-064)) | unit |
| Биллинг: сообщение с раундом(ами) `time.now` = 1 кредит (mode=credits) — server-side раунд не добавляет списаний | integration |
| Системный промт (chat и code) содержит статичную time.now-инструкцию; промт стабилен между запросами (prompt cache не инвалидируется — дата НЕ в промте) | unit |
| **E2E (реальный Anthropic):** запрос «какое сегодня число / какой день недели» в «чистом чате» без проекта → Claude вызывает `time.now`, отдаёт верную дату (не «2024») | e2e (`@pytest.mark.external`) |

> **tzdata-зависимость ([TD-019](100-known-tech-debt.md) Resolved 2026-06-10).** Тесты локального времени (`tz` → `local`/`timezone`) требуют tz-базы в тестовом окружении. tz-база обеспечена pure-Python зависимостью `tzdata` (`pyproject.toml`/`uv.lock`), входящей и в dev-, и в prod-окружение → валидный `tz` резолвится в тестах и в prod. UTC-кейсы tz-базы не требуют.

## Политика моков
- **PostgreSQL и Redis — реальные** (testcontainers). Не мокать БД.
- **Anthropic API, App Store Server API, KMS** — мокаются (respx / fakes). Реальные вызовы только в отдельном `@pytest.mark.external` наборе (вне CI по умолчанию).

## State-machine тестирование Policy Engine
Полная таблица переходов из [ADR-002](adr/ADR-002-access-policy-state-machine.md) покрывается параметризованными unit-тестами: декартово произведение {subscription: none/active/expired} × {trial_used: T/F} × {credits: 0/>0} × {byok: **missing**/disabled/invalid/valid} × {mode: credits/byok} → ожидаемый `allow|blockReason`. Ось `byok` перечислена по входу [ADR-002](adr/ADR-002-access-policy-state-machine.md) целиком — **четыре** значения; прежняя редакция называла три и при этом объявляла таблицу «полной». Дом перечня — [modules/policy-engine/09-testing.md](modules/policy-engine/09-testing.md), где ось названа так же; расхождение двух списков об одном произведении устранено. Кейс: `tests/unit/test_policy_engine.py::test_state_machine_full_matrix` (произведение строится по `list(ByokState)`, поэтому в прогон входят и расширенные статусы [ADR-016](adr/ADR-016-extended-byok-statuses.md), которых во входе ADR-002 нет).

## Структура
```
tests/
  unit/         # policy, conversion, schemas, crypto
  integration/  # endpoints + db + redis, respx для внешних
  e2e/          # сквозные сценарии
  conftest.py   # фикстуры: app, db container, redis container, jwt factory
```

## CI gate (см. 07-deployment.md)
PR не проходит, если: `ruff format --check` fail, `ruff check` fail, `mypy` fail, `pytest` fail, coverage < 80%.

## CRM request history (ADR-077, чтение — ADR-078)

Обязательные integration-сценарии:

- `audit_logs` с `billing_debit`/`policy_decision`/`chat_step` не появляются
  в `GET /v1/admin/users/{id}/requests`;
- **история ретроактивна:** при ПУСТОМ `request_logs` ход чата (`chat_steps` +
  списание в `ledger_transactions`) и `media_jobs` дают строки истории. Тест на
  это — единственная защита от повторения регрессии «пустая история после
  перевода чтения на новый журнал»;
- **ход tool-loop’а не размножается:** два `assistant`-шага с одним
  `message_step_id` и одним списанием дают РОВНО одну строку;
- успешный chat route создаёт одну completed-строку с реальным endpoint,
  duration и credits из debit текущего вызова;
- идемпотентный replay не приписывает существующее списание повторно;
- техническая ошибка сохраняет failed-строку после rollback основного scope;
- SSE mid-stream error завершает строку failed при transport HTTP 200;
- media submit создаёт queued/202, poll и reconciler идемпотентно обновляют ту
  же строку в completed/failed; refund не обнуляет tokens_spent;
- `provider_cost_usd` остаётся `null`, пока нет проверенного тарификатора;
- миграция `0023` upgrade/downgrade и индексы проверяются на PostgreSQL.

## Экономика и настройки инстанса ([ADR-099](adr/ADR-099-crm-admin-economics-and-instance-settings.md))

Полный перечень сценариев — [modules/admin/09-testing.md](modules/admin/09-testing.md#integration--экономика-и-настройки-инстанса-adr-099);
модульные `09-testing.md` затронутых модулей добавляют только свой путь. Здесь — требования,
без которых зелёный прогон **не доказывает** корректность выката.

| Требование | Уровень | Почему обычного теста мало |
|---|---|---|
| **Пустой оверлей = поведение до выката бит-в-бит** — параметризация по каждой тарифной строке и каждому из пяти путей начисления; ожидаемое значение **вычисляется** действующей формулой (`run_price()`, env-карта), а не копируется константой из документа | integration | Константа в фикстуре кодирует то же допущение, что и код: тест пройдёт и при разошедшемся дефолте. Диф-проверка: подмена дефолта обязана ронять кейс |
| **Сквозная цепь «правка → применение»**: `PATCH` admin-ручки → обновление снимка → **реальный** ход чата / submit генерации / пользовательская ручка возвращают новое значение | integration | Компонентный тест резолвера сам конструирует снимок и доказывает устройство потребителя, а не поставку данных ему («объявлено ≠ подключено») |
| **Достижимость наблюдаемости**: удаление строки эмиссии любой метрики **и любого из шести лог-событий** [ADR-099 §10](adr/ADR-099-crm-admin-economics-and-instance-settings.md) обязано ронять тест; серия обязана появляться в экспозиции `/metrics` после реального вызова. Перечень событий задан **признаком** («всякий `log_event` в `src/app/instance_config/` и в `src/app/admin/economics_service.py`»), число датировано 2026-09-08 | unit+integration | Юнит продюсера проходит и тогда, когда на рабочем пути метрика не эмитится. Перечень **имён** дважды оказывался неполным (половина пары `media_price_*`, затем `admin_overrides_snapshot_changed`) — признак переживает появление нового члена, список нет |
| **`admin_overrides_snapshot_changed` — обе стороны перехода** ([ADR-099 §2/§10](adr/ADR-099-crm-admin-economics-and-instance-settings.md)): правка, меняющая состав оверлеев ⇒ событие пишется; следующее обновление снимка **без** изменения состава ⇒ **не** пишется | integration | Кейс «событие когда-нибудь появилось» проходит и при эмиссии на каждом тике окна — то есть ровно при том дефекте (поток строк, в котором изменение не видно), ради которого запись сделана на изменении |
| **Численная модель легаси-множителей медиа**: для каждой видео-модели выведенная тройка на **дефолтной** таблице равна значениям реестра и воспроизводит все 46 ячеек поэлементно, `media_price_legacy_overquote` = 0; на произвольной правке — ни одного занижения перебором комбинаций | unit | Проверка «на глаз» уже дала ошибку: «максимум отношений» ломает дефолтную таблицу `kling-video-v3` в 10 из 26 комбинаций, и заметить это можно только счётом |
| **Видимость нормализации `general`** ([ADR-099 §8.1](adr/ADR-099-crm-admin-economics-and-instance-settings.md)): `PATCH chat.advertised_generation_modes` без `general` → `200`, и `general` присутствует в ответе `PATCH`, в последующем `GET` и в строке оверлея | integration | Кейс на read-time резолвере проходит и тогда, когда оператор своего `general` **не видит**: он проверяет второй барьер (значения из env и прямой записи в БД), а не то, что вернулось на правку. Молчаливое игнорирование ввода — тот же класс, что молчаливый приём `avatar_tokens` |
| **`reason` метрик и логов проверяется по ПРЕДИКАТУ, а не по факту инкремента** ([ADR-099 §10.0](adr/ADR-099-crm-admin-economics-and-instance-settings.md)): кейс на каждое значение **каждого продюсера**, падающий при подмене лейбла соседним | integration | «Счётчик вырос» проходит при **любом** лейбле. Ловушки, ради которых кейсы и пишутся: отказ, не являющийся ошибкой БД, обязан метиться `unexpected` (а не `db_error` — иначе дежурный уходит в базу при дефекте нашего кода); нарушение **объявленной** границы — `out_of_range` (`422`), а нарушение **необъявленной** нижней границы `tokens` — `undeclared_bound` (`400`), и кейс обязан падать при откате к первой паре; отказ из-за **неполноты источника продукта** — `source_kind_missing`, а не `unsupported_field` (последнее зарезервировано за `avatar_tokens`); строка-сирота в `admin_settings` — `unknown_id`, а не `type_mismatch`. ⛔ **Снятое значение обязано исчезнуть вместе с веткой:** `source_tokens_missing` после [ADR-099 §6.1](adr/ADR-099-crm-admin-economics-and-instance-settings.md) producer'а не имеет, и его появление на проде = откат нормы. Там, где лейбл и код меняются вместе (нижняя граница), это сказано явно; во всех прочих ловушках HTTP-код не меняется — проверяется лейбл |
| **Позитивный кейс rate limit на каждом из восьми admin-путей** (превышение → `429`) | integration | Лимит — явный вызов в хендлере, не middleware. Кейс «страница не даёт `429`» проходит и при полностью отсутствующем лимитере, то есть проверяет форму, а не факт |

**Изоляция теста охватывает НЕ только таблицы — правило по форме, а не перечнем.** Волна ввела
три таблицы-оверлея, и строка, оставленная одним тестом, молча меняла цену и настройки для всех
последующих: дефект не падает, а **тихо перекрашивает** соседние прогоны. Норма формулируется так,
чтобы пережить четвёртую таблицу:

> **Всё, что тест может изменить и что переживает его завершение, обязано сбрасываться между
> тестами — независимо от того, где это состояние живёт.**

Отсюда два следствия, и второе важнее первого, потому что его пропускают:

1. **новая операторская таблица обязана попадать в очистку** (`_TABLES`, `tests/conftest.py`) тем
   же проходом, что и миграция, которая её вводит — перечень таблиц не документируется здесь, чтобы
   не разойтись с кодом;
2. ⚠️ **`TRUNCATE` не лечит состояние процесса.** Снимок оверлеев
   ([ADR-099 §2](adr/ADR-099-crm-admin-economics-and-instance-settings.md)) — глобальная переменная,
   а не строка БД: пишущая ручка обновляет его сразу после коммита, и без явного сброса он пережил
   бы усечение таблиц и продолжил бы отдавать операторскую цену следующему тесту. Поэтому сброс
   снимка — **autouse**-фикстура, а не вызов в отдельных тестах. Правило действует на **любое**
   кэширующее состояние процесса, которое волна вводит: `lru_cache`, снимок, реестр в памяти.

**Гейт покрытия не меняется:** решение **не затрагивает** пакеты `src/app/policy`, `src/app/wallet`,
`src/app/byok` (`policy.engine.evaluate` уже принимает `required_credits` параметром,
`WalletService` — `amount`), поэтому требование **≥ 95 %** по ним новых кейсов не получает; новый
код покрывается общим гейтом **≥ 80 %**.
