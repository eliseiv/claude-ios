# 07 — Фазы реализации

## Phase 1 — Каркас (✅ выполнено)

- Реестр моделей `catalog.py`: пять моделей, два варианта на каждую, allowlist входных полей, имя поля с картинкой per-family, цена по умолчанию.
- Config: `FAL_API_KEY`, `FAL_QUEUE_BASE`, `FAL_TIMEOUT_SECONDS`, `MEDIA_MODEL_CREDITS`, `MEDIA_JOBS_PAGE_LIMIT`.
- Ошибка `media_generation_not_configured` (503).

## Phase 2 — Данные (✅ выполнено)

- Миграция `0018_media_jobs` (single head, expand-only) + ORM-модель `MediaJob`.
- `media_jobs` добавлена в TRUNCATE-список тестов, чтобы задачи не протекали между тестами.

## Phase 3 — Провайдер (✅ выполнено)

- `fal_client.py`: `submit`/`status`/`result` над queue API, схема `Authorization: Key`, маппинг ошибок в `502`/`503`/`422`/`429`, SSRF-guard на URL'ах опроса.

## Phase 4 — Use-cases и API (✅ выполнено)

- `service.py`: постановка (цена → списание → сабмит → задача в одной транзакции), опрос с переходами состояний, возврат кредитов при провале, нормализация результата.
- `repository.py`, `schemas/media.py`, роутер `/v1/media/*`, регистрация тега `Media` и роутера в `main.py`, wiring в `deps.py`.

## Phase 5 — Тесты (✅ выполнено)

Три unit-файла и один интеграционный — состав см. [09-testing.md](09-testing.md). Эндпоинты добавлены в `_ENDPOINT_TAG`/`_TAG_ORDER` контрактного теста OpenAPI.

## Phase 6 — Живая проверка (✅ выполнена локально 2026-08-04, ⏳ повторить на инстансе)

Локальный прогон с реальным ключом: все пять моделей дошли до `completed` в оба режима (text-only и с референсной картинкой), ассеты скачиваются, списание сошлось до кредита, ключ не утёк в логи и БД. Прогон выявил дефект «`422` при опросе» — исправлен, см. [09-testing.md](09-testing.md).

На инстансе повторить тот же порядок:

1. Задать `FAL_API_KEY` в `/opt/<instance>/.env`, перезапустить `api`.
2. `GET /v1/media/models` — каталог отдаётся, цены и наборы значений соответствуют ожидаемым.
3. По одному запуску на каждую из пяти моделей: `202` → опрос до `completed`, ссылка в `assets` открывается. Повторить с референсной картинкой (image-to-image / image-to-video).
4. Сверить `creditsCharged` со списанием в `GET /v1/wallet`, в том числе на масштабированных запросах (`numImages > 1`, длинное видео).
5. Негативный сценарий: заведомо отклоняемый промт → задача в `failed`, кредиты вернулись.
6. По результатам откалибровать `MEDIA_MODEL_CREDITS` под реальный биллинг fal ([Q-060-3](../../99-open-questions.md)).

## Phase 7 — Модерация UGC ([ADR-086](../../adr/ADR-086-ugc-moderation.md), ⏳ спроектирована, код не написан)

Порядок обязателен: **сначала env на всех инстансах, потом код** — деплой на прод автоматический (CI push→deploy), а фича включена дефолтом.

1. **devops (до мержа кода):** задать `MODERATION_API_KEY` в `/opt/<dir>/.env` **каждого** инстанса, где `OPENAI_API_KEY` пуст (все `LLM_PROVIDER=anthropic`), перезапуск не требуется до выката кода. Пункт внесён в [prod-checklist](../../07-deployment.md#prod-readiness-checklist-must-configure-before-launch).
2. **Config:** `MODERATION_ENABLED`, `MODERATION_API_KEY`, `MODERATION_MODEL`, `MODERATION_BASE_URL`, `MODERATION_TIMEOUT_SECONDS`, `MODERATION_MAX_RETRIES`, `MODERATION_BLOCK_CATEGORIES`, `MODERATION_TEXT_MAX_CHARS`, `MODERATION_FAIL_OPEN` — значения и дефолты в [07-deployment.md §Конфигурация (env)](../../07-deployment.md#конфигурация-env).
3. **Сервис модерации** (`src/app/moderation/`): один вызов `omni-moderation-latest` на текст + изображения запроса, вычисление вердикта по предикату [ADR-086 §6](../../adr/ADR-086-ugc-moderation.md), лог `moderation_outcome`, метрики `moderation_decisions_total`/`moderation_errors_total`. Ошибки провайдера → доменные `moderation_unavailable`/`moderation_not_configured`.
4. **Миграция** (expand-only, следующий свободный номер, single head): `media_jobs.moderation JSONB NULL`, без backfill.
5. **Media:** вызов в `submit` **до** `wallet.consume`; вызов в `_advance` для `kind=image` **до** `mark_completed`; блокировка выхода → `{"assets": []}` + возврат + `mark_failed` + отсутствие push.
6. **Uploads:** вызов в `upload_reference_image` до обращения к хранилищу провайдера.
7. **Схемы:** поле `moderation` в `MediaJobResponse` (лента получает автоматически — тот же тип); ошибки `content_policy_violation`/`moderation_unavailable`/`moderation_not_configured` в `errors.py` и в `responses` роутов.
8. **Chat:** вызов в `ChatOrchestrator.run` на ходах **с вложениями** — [chat-orchestrator/07-implementation-phases.md](../chat-orchestrator/07-implementation-phases.md).
9. **Тесты:** [09-testing.md §Модерация UGC](09-testing.md), включая diff-тест порядка «модерация до списания» и проверку недостижимости заблокированных ассетов.

## Phase 8 — Дедлайн задачи и одна сборка сервиса ([ADR-105 §B](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md), реализована `cbed6ca`)

Код — коммит `cbed6ca` (`src/app/config.py`, `src/app/deps.py`, `src/app/media_generation/{fal_client,reconciler,repository,service}.py`, закомментированная строка в `.env.prod.example`); тесты — `tests/integration/test_media_deadline_adr105.py` (14 функций `test_`). Ревью не проходило.

1. **Config:** `MEDIA_JOB_DEADLINE_SECONDS` (дефолт `21600`, `<= 0` → дефолт). **devops:** закомментированная строка в `.env.prod.example` рядом с `MEDIA_RECONCILE_*`; в живые `.env` не вписывать — действует дефолт кода.
2. **До выката — по каждому инстансу, только агрегаты:** длительности завершённых задач (запрос [ADR-105 §B3](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)) — максимум больше половины дедлайна поднимает дефолт; число и сумма кредитов незавершённых задач старше дедлайна (запрос [ADR-105 §B9](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)) — это разовый возврат первого тика.
   **Замер выполнен 2026-09-11** (orchestrator волны, агрегаты по всем 41 инстансу, оба запроса):
   - **незавершённые задачи старше 6 ч** (первый тик вернёт их кредиты): velunixa — 19 (382 кредита, старейшая 2026-08-30), devsupplyr — 1 (14 кредитов, 2026-09-04), остальные 39 инстансов — 0;
   - **длительность `completed`:** максимум 2310 с (velunixa; p99.9 там 525 с), у прочих инстансов ≤ 740 с — **кроме** webmoria: одна задача `veo-3.1` с 341590 с (создана 2026-08-07 12:30 UTC, `completed` и `push_sent_at` — 2026-08-11 11:23 UTC). Это не длительность генерации: согласователь появился в коммите `10e78c6` (2026-08-11 10:11 UTC) и доопросил задачу первым тиком, то есть посылка запроса §B3 «`updated_at` у `completed` — длительность плюс не более одного опроса» у задачи, созданной до согласователя, не выполняется;
   - **решение orchestrator'а волны по замеру:** дефолт `21600` **не поднимается** — без названного артефакта максимум по флоту 2310 с меньше половины дедлайна (10800 с).
3. **`MediaGenerationService._advance`:** опрос всегда; опрос без конечного состояния у задачи старше дедлайна → `_fail(error="generation did not complete in time")`; событие `media_generation_deadline_exceeded` с `lastObservation` по предикатам ADR; `FalClient._raise_for_status` прикрепляет HTTP-статус к поднимаемому исключению.
4. **Согласователь:** сервис — той же функцией, что request-путь (`deps.build_media_generation_service`, с `request_logs` и `moderation`; `deps.get_media_generation_service` — её обёртка-зависимость FastAPI); при пустом `FAL_API_KEY` — только просроченные (`MediaJobsRepository.list_non_terminal(created_before=…)`), без опроса; `media_reconcile_job_error` + `exceptionClass`.
5. **Тесты:** [09-testing.md §Дедлайн задачи](09-testing.md#integration--дедлайн-задачи-adr-105).

## Post-MVP (не в этой поставке)

- ~~**Фоновая доводка «зависших» задач**~~ — **сделано:** фоновый согласователь ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md), закрыл [Q-060-2](../../99-open-questions.md)); предел жизни задачи, на которую провайдер не даёт конечного ответа, — Phase 8 ([ADR-105 §B](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)).
- **Собственное хранение ассетов** — сейчас отдаются ссылки CDN провайдера; их срок жизни на его стороне ([Q-060-1](../../99-open-questions.md)).
- **Второй провайдер** — контракт `/v1/media/*` уже провайдер-агностичен (нормализация результата + реестр), понадобится второй клиент и признак провайдера в реестре.
- **Генерация как инструмент tool-loop** — чтобы ассистент мог сгенерировать картинку внутри диалога; сейчас это отдельная поверхность.
