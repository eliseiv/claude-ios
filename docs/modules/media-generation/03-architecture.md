# 03 — Архитектура

## Файлы

| Файл | Роль |
|---|---|
| `src/app/media_generation/catalog.py` | реестр моделей: публичный id → endpoint fal, allowlist входных полей, имя поля с картинкой, цена по умолчанию |
| `src/app/media_generation/fal_client.py` | исходящий httpx-клиент fal: `submit` / `status` / `result` очереди и `upload` хранилища, маппинг ошибок; HTTP-статус ответа прикрепляется к поднятому исключению, читается `upstream_status_of` ([ADR-105 §B5](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)) |
| `src/app/media_generation/repository.py` | персистентность `media_jobs`, все запросы в скоупе владельца — кроме `list_non_terminal(limit, created_before=None)` для согласователя (старейшие незавершённые, без скоупа; `created_before` сужает по возрасту) |
| `src/app/media_generation/service.py` | use-cases: `submit` (модерация входа → цена → списание → сабмит → задача), `get_job` (опрос + переходы + пост-модерация + возврат + дедлайн), `advance` (то же для уже загруженной задачи — согласователь), `list_jobs` (лента), `delete_job`, `upload_reference_image` |
| `src/app/media_generation/reconciler.py` | фоновый согласователь `reconcile_once` ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md), [ADR-105 §B6/§B7](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)) — см. [§Согласователь](#согласователь-одна-сборка-сервиса-adr-105-b6) |
| `src/app/moderation/service.py` | клиент модерации UGC ([ADR-086](../../adr/ADR-086-ugc-moderation.md)): один вызов `omni-moderation-latest` на текст+изображения, вычисление вердикта, метрики/лог. Общий для media, chat и uploads |
| `src/app/media_generation/cursor.py` | непрозрачный keyset-курсор ленты `(created_at, id)` |
| `src/app/schemas/media.py` | схемы запросов/ответов (camelCase, `extra=forbid`) |
| `src/app/api_gateway/routers/media.py` | роутер `/v1/media/*`, rate limit, проекция в схемы ответа |
| `migrations/versions/20260804_0018_media_jobs.py` | миграция таблицы |
| `migrations/versions/20260805_0019_media_jobs_edit_chain.py` | цепочка правок: `parent_job_id`, `input_image_urls` |

**[ADR-108](../../adr/ADR-108-media-generation-via-proxy.md) (реализовано) добавляет:** исходящий клиент прокси (`POST {PROXY_BASE}/api/v1/tasks`), модуль маршрутизации (публичная модель → `fal`/`kie`/`sosana` + endpoint вендора, цены маршрутов), подпись и разбор колбэка, отдельный роутер `POST /v1/media/webhooks/proxy/{jobId}` (вне гейта, вне OpenAPI), метод репозитория «строка по id под `FOR UPDATE`» и миграцию `provider`/`vendor_price`/`pending_result`. Имена модулей образца: `proxy_client.py`, `routing.py`, `webhook.py`, `routers/media_webhooks.py` (ai-media-upscaler). Порядок шагов — [§Транспорт через прокси](#транспорт-через-прокси-adr-108).

**[ADR-109](../../adr/ADR-109-media-asset-local-storage-30d.md) (код в `main` (`f90d871`), выкачен (CI `36015691825` на `48f9018`, джоб `ssh deploy` — `success`)) добавит:** фоновый цикл хранилища ассетов (сохранение + очистка) рядом с согласователем, ветку «своя копия» в download-роуте и колонки состояния копии в `media_jobs` — [§Своё хранение результатов](#своё-хранение-результатов-adr-109). Имена модулей выбирает `backend`.

Wiring — `deps.build_media_generation_service` (единственная сборка сервиса — request-путь и согласователь, [ADR-105 §B6](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)), `deps.get_media_generation_service` (её обёртка-зависимость FastAPI), `deps.get_fal_client`.

## Поток постановки задачи

```
POST /v1/media/images|videos
  ├─ rate limit (enforce_other_limits)          → 429
  ├─ схема запроса (StrictModel)                → 422
  ├─ catalog: resolve model id                  → 422 (неизвестна / не тот kind)
  ├─ sourceJobId? → ассеты родителя как референс   → 404 (чужой) / 422 (не тот статус/kind)
  ├─ catalog: variant = image_variant | text_variant   (наличие картинки решает endpoint)
  ├─ валидация значений против набора ВАРИАНТА    → 422 (до любого списания)
  ├─ МОДЕРАЦИЯ ВХОДА: prompt + КЛИЕНТСКИЕ imageUrls   → 422 content_policy_violation
  │     (ADR-086 §4; ассеты из sourceJobId уже проверены и не перепроверяются;
  │      провайдер модерации недоступен → 503 moderation_unavailable, fail-closed)
  ├─ rehost референса (video)                   → 502 (копия упала — бесплатно)
  ├─ resolve_values: дефолты варианта для полей, влияющих на цену   (ADR-061 §3)
  ├─ cost = f(ЭТИХ ЖЕ значений)                 (что тарифицируем — то и отправляем)
  ├─ jobId = uuid4()                            (нужен как ключ идемпотентности раньше строки)
  ├─ wallet.consume(cost, key=media-gen:{jobId})→ 409 insufficient_credits
  ├─ fal.submit(endpoint, payload)              → 502 / 503 / 422 / 429
  └─ INSERT media_jobs(status='queued', moderation=<вердикт входа>)
        ↓
     session_scope commit  ── всё выше в ОДНОЙ транзакции
```

Диаграмма выше — **полный** порядок шагов сабмита; шаг модерации входа добавлен [ADR-086](../../adr/ADR-086-ugc-moderation.md) и обязан присутствовать здесь так же, как в ADR.

**[ADR-108](../../adr/ADR-108-media-generation-via-proxy.md) :** при `proxy_configured` шаг `fal.submit` заменяется перебором маршрутов прокси (`callbackUrl` строится из `jobId` до вызова), а `INSERT` пишет `provider`/`status_url=''`/`response_url=''`; все прочие шаги, их порядок и граница транзакции — те же. Полный порядок — [§Транспорт через прокси](#транспорт-через-прокси-adr-108).

> **`409 insufficient_credits` на шаге `wallet.consume` отменяет строку списания ТОЛЬКО откатом транзакции** ([wallet-ledger/03-architecture.md §consume](../wallet-ledger/03-architecture.md)): строка вставляется до балансового гейта. Отсюда: перехватить этот отказ и вернуть управление штатно — значит **закоммитить списание, которого не было**, при том что `INSERT media_jobs` (строка 39) не выполнялся и задачи нет. По REST-ручкам `/v1/media/*` отказ долетает наружу и откат происходит; **носитель дефекта — мягкий отказ media-инструмента в tool-loop чата**, где ход намеренно не роняется ([ADR-068 §1](../../adr/ADR-068-media-generate-chat-tools.md)): [TD-048](../../100-known-tech-debt.md).

Списание, сабмит и вставка живут в одной request-транзакции (`session_scope` коммитит один раз в конце). Отсюда два инварианта:

- **сабмит упал → списание откатилось**: пользователь не платит за запуск, который провайдер не принял;
- **строка `media_jobs` существует ⇒ за неё заплачено и провайдер ею владеет**.

**Инвариант модерации входа ([ADR-086 §4](../../adr/ADR-086-ugc-moderation.md)):** проверка стоит **до** `wallet.consume` — иначе пользователь платит за контент, который заведомо будет отклонён. **Контраст (обе стороны помечены):** пост-модерация результата (см. поток опроса ниже) идёт **после** списания по построению — раньше вердикта о выходе не существует — и потому **обязана вернуть кредиты**. Правило «до списания» на неё не переносится; правило «вернуть кредиты» на пре-модерацию не переносится (там списания ещё не было).

Порядок «сначала списать, потом сабмитить» выбран намеренно: обратный порядок оставлял бы оплаченные запуски без строки при отказе БД. При текущем порядке худший случай — осиротевший запуск у провайдера, за который пользователь не заплатил.

## Поток опроса

```
GET /v1/media/jobs/{jobId}   (тот же путь _advance — у фонового согласователя, ADR-067)
  ├─ repo.get(job_id, user_id)          → 404 (чужая/нет — неотличимо)
  ├─ status ∈ {completed, failed}?      → ответ из БД, провайдер не дёргается
  └─ fal.status(status_url)
       ├─ 422 на status/result          → wallet.grant(key=media-refund:{jobId}) → mark_failed (текст fal)
       ├─ 404 на status (UpstreamJobGoneError) → wallet.grant → mark_failed
       ├─ COMPLETED  → fal.result(response_url) → нормализация
       │                 ├─ нет assets  → трактуем как провал (см. ниже)
       │                 └─ есть assets → ПОСТ-МОДЕРАЦИЯ (только kind=image, ADR-086 §5)
       │                       ├─ blocked → assets ОТБРАСЫВАЮТСЯ, moderation=blocked,
       │                       │            wallet.grant(key=media-refund:{jobId}),
       │                       │            mark_failed(error="content_policy_violation"),
       │                       │            media-ready push НЕ шлётся
       │                       ├─ flagged → mark_completed, ассеты выдаются, возврата НЕТ
       │                       └─ passed  → mark_completed (как раньше)
       │                          (ADR-109 §2: при включённом хранении mark_completed
       │                           ещё ставит asset_store_status='pending' + assets_expire_at;
       │                           скачивание — ПОСЛЕ коммита, фоновым циклом, §Своё хранение)
       ├─ FAILED / CANCELED → wallet.grant(key=media-refund:{jobId}) → mark_failed
       ├─ IN_QUEUE / IN_PROGRESS → mark_running          ┐ опрос НЕ дал конечного
       └─ любое иное исключение (5xx, 429, 401/403,      │ состояния:
          таймаут, обрыв, битый JSON — на status или     │ ДЕДЛАЙН (ADR-105 §B2)
          на result; недоступна пост-модерация;          │
          исключение нормализации результата)            ┘
             ├─ now − created_at > MEDIA_JOB_DEADLINE_SECONDS
             │     → media_generation_deadline_exceeded → wallet.grant(key=media-refund:{jobId})
             │       → mark_failed(error="generation did not complete in time") → 200 failed
             └─ иначе → как было: mark_running / исключение наверх (клиенту 502/503/429,
                        согласователю — media_reconcile_job_error), следующий опрос повторит
```

Диаграмма выше — **полный** порядок опроса, включая шаг пост-модерации ([ADR-086](../../adr/ADR-086-ugc-moderation.md)), терминальные `422`/`404` на опросе (факт кода: `MediaGenerationService._advance` ловит `ValidationFailedError` и `UpstreamJobGoneError`; `404` — коммит `5ebf963`, в [ADR-060 §3](../../adr/ADR-060-media-generation-fal.md) не значился) и ветку дедлайна **[ADR-105 §B2](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md) (реализована в `cbed6ca`: `MediaGenerationService._close_if_overdue`, зовётся из `_advance` только когда опрос не дал конечного состояния; до `cbed6ca` нижняя ветка шла без дедлайна, и задача, на которую fal не давал конечного ответа, опрашивалась вечно без возврата кредитов)**. Каждой ветке дедлайна `_advance` передаёт `lastObservation` по месту, где опрос остановился: исключение `FalClient.status`/`FalClient.result` → `upstream_error` при заданном ключе и `not_configured` при пустом (`_fal_failure_observation`), нетерминальный статус → `upstream_pending`, исключение нормализации результата → `internal_error`, исключение `_moderate_output` → `moderation_unavailable`; значения — константы `OBSERVATION_*` в `src/app/media_generation/service.py`, текст ошибки — `DEADLINE_EXCEEDED_ERROR`.

Диаграмма выше — поток **legacy-задачи** (`provider = ''`) по [ADR-108](../../adr/ADR-108-media-generation-via-proxy.md); у proxy-задачи ветки `fal.status`/`fal.result` нет — [§Транспорт через прокси](#транспорт-через-прокси-adr-108).

**Дедлайн задачи ([ADR-105 §B](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)).** Любая строка `media_jobs` достигает `completed`/`failed` не позже `created_at + MEDIA_JOB_DEADLINE_SECONDS` (дефолт `21600`, 6 ч). Предикат двусторонний: **(а)** задача старше дедлайна, опрос не дал конечного состояния по ЛЮБОЙ причине ⇒ `failed` + возврат; **(б)** задача моложе дедлайна правилом не трогается, какой бы ни была ошибка, а задача старше дедлайна, чей опрос дал `COMPLETED` с ассетами или `FAILED`, получает этот исход, а не текст дедлайна (опрос выполняется всегда — «последний шанс»). Мерило — возраст, а не число попыток: частота опроса зависит от клиента, интервала и числа реплик. Значение `<= 0` приводится к дефолту.

**Заблокированный результат не сохраняет ассеты.** `media_jobs.result` пишется как `{"assets": []}` — иначе файл остался бы достижим по signed-URL download-роуту ([ADR-085](../../adr/ADR-085-media-asset-download-proxy.md)), и блокировка была бы декоративной. У `kind=video` пост-модерации нет (провайдер модерации не принимает видео) — вердикт видео-задачи отражает только вход, `stage: "input"` ([Q-086-2](../../99-open-questions.md)).

**Недоступность провайдера модерации на опросе** ведёт себя как транзиентная ошибка апстрима: задача остаётся non-terminal, `mark_completed` не выполняется, следующий опрос (или reconciler, [ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)) доберёт исход — **но не позже дедлайна задачи**: по его истечении `failed` с возвратом, ассеты не выдаются ([ADR-105 §B2](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md), `lastObservation = moderation_unavailable`). Отдавать ассеты «пока модерация недоступна» запрещено — это и есть fail-open, отвергнутый в [ADR-086 §7](../../adr/ADR-086-ugc-moderation.md); при `MODERATION_FAIL_OPEN=true` (аварийный режим оператора) задача завершается с `moderation.status = "unchecked"`.

`COMPLETED` без пригодного URL трактуется как провал: с точки зрения пользователя разницы между «упало» и «завершилось без результата» нет, а кредиты в обоих случаях должны вернуться.

Возврат идемпотентен дважды: ключом ledger `media-refund:{jobId}` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)) и флагом `credits_refunded` в строке — флаг лишь избавляет от повторного вызова, гарантию даёт ключ.

`GET /v1/media/jobs` (лента) провайдера **не опрашивает**: N задач не должны разворачиваться в N исходящих вызовов. Пагинация keyset-курсорная по `(created_at, id)`: лента растёт с головы, и при `offset` вставка новой задачи между запросами дала бы дубли и пропуски.

`DELETE /v1/media/jobs/{jobId}` удаляет только нашу строку и только у терминальной задачи: возврат кредитов привязан к строке и срабатывает при опросе, поэтому удаление незавершённой уничтожило бы единственное место, где этот возврат может произойти ([ADR-063 §4](../../adr/ADR-063-media-feed-edit-chains-and-job-deletion.md)). С [ADR-109 §6](../../adr/ADR-109-media-asset-local-storage-30d.md) (код в `main` (`f90d871`), выкачен (CI `36015691825` на `48f9018`, джоб `ssh deploy` — `success`)) удаление строки делает сиротой и нашу копию результата на диске — её стирает фоновая очистка, сам запрос диска не касается.

## Цена генерации после [ADR-099](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md)

Цена **одного запуска** резолвится по координатам варианта (модель + разрешение + длительность +
звук) в порядке **оверлей `admin_tariffs` → `MEDIA_MODEL_CREDITS` → формула реестра
(`run_price()`)**. Оверлей заполняется оператором из CRM и **всегда серверный** — цена по-прежнему
**никогда** не приходит из тела запроса.

- **Пустой оверлей = сегодняшние цены бит-в-бит.** Дефолт каждой ячейки вычисляется той же
  формулой `run_price()`, а не переписывается константой;
- **у видео ячейка — полная цена запуска**, а не цена пачки: `ceil(23×2×1.5) = 69`, тогда как
  `2 × ceil(23×1.5) = 70`, и пачечная ячейка сдвинула бы действующее списание на единицу;
- **у фото ячейка — цена одного изображения**; итог = `ячейка × numImages`, и именно это объявлено
  единицей `image` в admin-контракте;
- ⚠️ **`GET /v1/media/models` обязан оставаться согласованным с ячейками.** Аддитивный `prices[]`
  точен всегда; легаси-тройка выводится из ячеек как **минимальная не занижающая** — порядок
  вывода `credits` → `resolutionMultipliers[r]` → `audioMultiplier` (наименьшее кратное `1/20`,
  проверка в целочисленной арифметике) задан [ADR-099 §4.4](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md).
  ⛔ **«Максимум отношений ячеек» — неверная реализация:** отношения берутся от уже округлённых
  величин, и на **дефолтной** таблице `kling-video-v3` дают `1.5217` вместо `1.5` (клиент показал
  бы 70 вместо 69 в 10 из 26 комбинаций). На дефолтах верный вывод даёт **ноль** расхождений, и
  `media_price_legacy_overquote` равна нулю — это её нормативное состояние, а не «обычно ноль»;
- правка применяется в течение окна `ADMIN_OVERRIDES_REFRESH_SECONDS` (дефолт 30 с).

## Реестр моделей

Каждая модель объявляет **два варианта** — prompt-only (`text_variant`) и «с референсным изображением» (`image_variant`), потому что у fal это разные endpoint'ы. Вариант выбирается по наличию картинки в запросе.

Каждый вариант несёт **allowlist полей**, которые уходят наверх, и **дефолты** влияющих на цену полей. `resolve_values` подставляет дефолт вместо неприсланного поля **до** расчёта цены, и одно и то же значение идёт и в цену, и в запрос ([ADR-061 §3](../../adr/ADR-061-fal-price-calibration-and-priced-defaults.md)): дефолты провайдера в ценообразовании не участвуют, потому что они не наши и дороже наших. `build_fal_input` отбрасывает оставшиеся `None` и всё, чего нет в allowlist. Это не косметика: fal отбивает неизвестные ключи, а входные схемы моделей различаются — у Veo нет `cfg_scale`, у Kling нет `resolution`, у image-to-video Kling нет `aspect_ratio`.

Там же лежат **наборы допустимых значений** `aspect_ratio`/`resolution`/`duration` — именно на варианте, а не на модели, потому что они различаются между режимами: Veo в text-to-video не принимает `aspect_ratio: "auto"`, а в image-to-video принимает. Валидация идёт против варианта, поэтому неверное значение отбивается до списания вместо оплаченного upstream-отказа. `GET /v1/media/models` отдаёт эти наборы как `modes[]`, чтобы UI строил контролы по режиму.

Имя поля с картинкой хранится в реестре, потому что наверху оно не унифицировано: `image_urls` (список) у image-моделей, `image_url` у Kling 2.5 и Veo, `start_image_url` у Kling v3. Благодаря этому сервис и схемы остаются модель-агностичными.

Значения реестра сверены с опубликованными схемами fal (`fal.ai/api/openapi/queue/openapi.json?endpoint_id=…`) — это источник истины при добавлении модели или обновлении набора.

## Клиент провайдера

Паттерн [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md): per-call `httpx.AsyncClient`, таймаут из `FAL_TIMEOUT_SECONDS`, ключ в собственной схеме fal `Authorization: Key <FAL_API_KEY>` (не `Bearer`), ключ не логируется.

Используется **queue** API (`FAL_QUEUE_BASE`, дефолт `https://queue.fal.run`), а не синхронный `fal.run`: минутные видео-генерации в синхронный HTTP не укладываются.

URL'ы опроса берутся из ответа на сабмит и **персистятся**: для вложенных endpoint'ов вида `kling-video/v3/pro/text-to-video` очередная тропа не выводится из одного идентификатора. Так как это URL, пришедший из внешней системы, перед каждым запросом проверяется префикс `FAL_QUEUE_BASE` (SSRF-guard); не прошёл — используется канонический вид, а не чужой хост.

Раздел описывает прямой клиент fal. С [ADR-108](../../adr/ADR-108-media-generation-via-proxy.md) он остаётся для загрузок, перехоста, features-загрузок, опроса legacy-задач и прямой ветки сабмита на инстансе без `PROXY_API_KEY`; сабмит через прокси — `Authorization: Bearer <PROXY_API_KEY>`, таймаут `PROXY_TIMEOUT_SECONDS`, ключ не логируется.

Маппинг ошибок — см. [ADR-060 §3](../../adr/ADR-060-media-generation-fal.md). Единственное исключение из правила «upstream наверх не проксируем» — `422`: сообщение fal называет проблемный параметр, секретов не содержит и полезно клиенту; текст обрезается до 500 символов и сплющивается в одну строку.

## Нормализация результата

Форма ответа провайдера различается (`images: [{url, content_type, file_name}]` у изображений, `video: {url}` у видео) и приводится на границе к стабильной `{assets: [{url, contentType, fileName}]}`. Вендорные имена полей не попадают ни в БД, ни к клиенту — замена провайдера не меняет контракт `/v1/media/*`.

## Наблюдаемость

Структурные события (`log_event`, allowlist полей; ключ провайдера никогда не логируется):

| Событие | Когда |
|---|---|
| `media_generation_submitted` | задача принята: `jobId`, `model`, `kind`, `credits`, `falEndpoint` |
| `media_generation_completed` | задача завершилась: `jobId`, `model`, число ассетов |
| `media_generation_failed` | провал: `jobId`, `model`, сколько кредитов возвращено (WARNING) |
| `moderation_outcome` | вердикт модерации ([ADR-086 §10](../../adr/ADR-086-ugc-moderation.md)): `surface`, `stage`, `decision`, `categories`, `userId`, `jobId`, `provider`, `model`, `latencyMs`. **Запрещено:** текст промпта, байты/base64 изображения, URL ассета целиком, ключ модерации |
| `media_generation_deleted` | задача убрана из ленты: `jobId`, `model`, статус на момент удаления |
| `fal_upload_outcome` | референсное изображение сохранено у провайдера: размер, mediaType |
| `fal_submit_outcome` | сабмит принят провайдером |
| `fal_call_outcome` | ошибка исходящего вызова: `reason`, `falEndpoint`, `upstreamStatus`. **`jobId` нет** — атрибуция к задаче по этому событию невозможна; её даёт `media_reconcile_job_error` |
| `media_reconcile_job_error` | продвижение задачи согласователем завершилось исключением (WARNING): `jobId`, `exceptionClass` — имя класса исключения, не текст ([ADR-105 §B5](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md); поле добавлено в `cbed6ca`, `reconcile_once`) |
| `proxy_submit_outcome` / `proxy_call_outcome` / `media_generation_route_fallback` / `media_webhook_outcome` | [ADR-108 §10](../../adr/ADR-108-media-generation-via-proxy.md): принятие задачи прокси, ошибка исходящего вызова прокси, откат на следующий маршрут, исход колбэка (значения `outcome` и их предикаты — в ADR). `media_generation_submitted` / `media_feature_submitted` получают `proxyService`. **Запрещено:** `PROXY_API_KEY`, секрет подписи, значение `token`, тело колбэка целиком, URL ассета целиком |
| `media_generation_deadline_exceeded` | [ADR-105 §B5](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md), реализовано в `cbed6ca` (`MediaGenerationService._close_if_overdue`). Задача доведена до `failed` по дедлайну (WARNING, пишется ветка дедлайна `_advance` перед `_fail`): `jobId`, `model`, `ageSeconds`, `lastObservation` ∈ `upstream_error` \| `upstream_pending` \| `moderation_unavailable` \| `not_configured` \| `internal_error` (предикаты — в ADR), `upstreamStatus` (только когда исключение fal его несёт — `upstream_status_of`, см. ниже). Следом — `media_generation_failed`. `not_configured` через HTTP-ручку не наблюдаемо: без `FAL_API_KEY` роутер `/v1/media` отвечает `503` до сервиса ([ADR-105 §B5](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md), уточнение факта); его пишет согласователь |

## Согласователь: одна сборка сервиса ([ADR-105 §B6](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md))

`reconcile_once` (`src/app/media_generation/reconciler.py`) собирает `MediaGenerationService` **той же функцией и с тем же набором зависимостей**, что request-путь (`repo`, `fal`, `wallet`, `settings`, `push`, `request_logs`, `moderation`): это `deps.build_media_generation_service(session, request_logs)` (`src/app/deps.py`) — единственная сборка сервиса; `deps.get_media_generation_service` — её обёртка-зависимость FastAPI для request-пути, согласователь зовёт её напрямую с `deps.get_request_log_writer(session)`. Реализовано в `cbed6ca`; до него (на `f8f4b37`) согласователь собирал сервис сам и **без** `request_logs` и `moderation` — строка `request_logs` задачи, доведённой согласователем, оставалась `queued` ([ADR-077 §3](../../adr/ADR-077-crm-request-logs.md) не выполнялся), а картинка, готовность которой обнаружил согласователь, выдавалась **без пост-модерации** ([ADR-086 §5](../../adr/ADR-086-ugc-moderation.md) не выполнялся). При пустом `FAL_API_KEY` согласователь не опрашивает, но задачи старше дедлайна доводит до `failed` (`lastObservation = not_configured`, [ADR-105 §B7](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)): выборка сужается по возрасту `MediaJobsRepository.list_non_terminal(limit=…, created_before=now − MEDIA_JOB_DEADLINE_SECONDS)`, и каждая взятая задача проходит обычный `advance` — `FalClient` отказывает до запроса (`FalClient._headers`), ветка дедлайна её закрывает; до `cbed6ca` `reconcile_once` при пустом ключе возвращал `0` до выборки. Выборка — по-прежнему старейшие первыми; дедлайн ограничивает сверху, сколько в голове пачки может лежать мёртвых задач.

**HTTP-статус fal на исключении ([ADR-105 §B5](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)).** `FalClient._raise_for_status` и `FalClient._upstream_error` прикрепляют HTTP-статус ответа fal к поднимаемому исключению атрибутом `upstream_status` (`_with_upstream_status`, `src/app/media_generation/fal_client.py`); читает его `upstream_status_of(exc)` — `None` у таймаута, обрыва, битого тела и пустого ключа (ответа HTTP не было). Ветка дедлайна пишет `upstreamStatus` в событие только при не-`None`.

## Транспорт через прокси (ADR-108)

Норма — [ADR-108](../../adr/ADR-108-media-generation-via-proxy.md); здесь — полный порядок шагов для точки чтения реализации. Классификатор: **proxy-задача ⇔ `provider <> ''`**, legacy-задача ⇔ `provider = ''` (опрос fal по разделам выше). Контраст: `provider = 'fal'` — fal **через прокси** (колбэк, опроса нет); `provider = ''` — fal **напрямую** (опрос).

**Сабмит (proxy_configured):**

```
POST /v1/media/images|videos | submit_custom | chat-tool media.generate_*
  ├─ … все шаги §Поток постановки задачи до wallet.consume — БЕЗ изменений
  ├─ SAVEPOINT { wallet.consume → маршруты → INSERT } — отказ внутри откатывает списание
  │     на ЛЮБОМ вызывающем, в том числе в tool-loop чата (ADR-108 §3.1)
  ├─ routes = маршруты ADR-108 §2 по возрастанию цены (fal есть всегда; последний на дефолтах,
  │          первый — если MEDIA_VENDOR_PRICES сделал его дешевле; sosana/kie — только по §2.1)
  ├─ callbackUrl = https://{SERVICE_DOMAIN}/v1/media/webhooks/proxy/{jobId}?token=HMAC(jobId)
  ├─ для route in routes: ProxyClient.submit(service, endpoint, payload, callbackUrl)
  │     ├─ таймаут / connect к прокси → 502, без отката на следующий маршрут
  │     ├─ 429 / 5xx / 402 / 400 без валидации → следующий маршрут
  │     ├─ 422 (или 400 с валидацией) у sosana/kie → следующий маршрут
  │     ├─ 422 у fal → 422 validation_error            ┐
  │     └─ 401/403 → 503 media_generation_not_configured ┘ стоп, списание откатывается
  │     (маршруты исчерпаны → последний 429 | 502, списание откатывается)
  └─ INSERT media_jobs(provider, fal_endpoint=<endpoint варианта>, fal_request_id=<id прокси>,
                      status_url='', response_url='', status='queued', …)
        ↓ session_scope commit — ОДНА транзакция, как сегодня
```

**Колбэк `POST /v1/media/webhooks/proxy/{jobId}`:**

```
  ├─ token невалиден                  → 401 (БД не читается)
  ├─ тело не JSON-объект              → 422
  ├─ SELECT … FOR UPDATE по id; нет строки или provider = '' → 404
  ├─ status ∈ {completed, failed}     → 200 no-op (media_webhook_outcome=duplicate_terminal)
  ├─ pending_result непуст (шаг 0 ADR-108 §4.3 — результат уже получен)
  │     ├─ outcome = completed → результат НЕ перезаписывается → SAVEPOINT: ОБЩИЙ ПУТЬ ЗАВЕРШЕНИЯ
  │     │                        с сохранённого pending_result (исходы — как в ветке ниже)
  │     └─ outcome = failed | pending → игнор → 200 (media_webhook_outcome=result_already_received)
  ├─ outcome = pending                → mark_running → 200
  ├─ outcome = failed                 → _fail(текст вендора) → 200
  └─ outcome = completed              (pending_result пуст)
        ├─ нормализация (форма fal → действующий _normalize_result; иначе сбор URL)
        ├─ URL не https / хост вне FAL_UPLOAD_HOST_SUFFIXES ∪ MEDIA_RESULT_HOST_SUFFIXES → отброшен
        │     └─ ассетов нет → _fail("generation produced no output") → 200
        ├─ UPDATE pending_result, vendor_price
        └─ SAVEPOINT: ОБЩИЙ ПУТЬ ЗАВЕРШЕНИЯ
              пост-модерация (image) → blocked? _blocked_by_moderation
              → completion handler → mark_completed (pending_result := NULL;
                 ADR-109 §2: + asset_store_status='pending', assets_expire_at)
              → request_logs.finish_media → media_generation_completed → push (claim push_sent_at)
              ├─ задача completed → 200 (media_webhook_outcome=completed)
              ├─ задача failed (blocked / ValidationFailedError handler) → 200 (completion_failed)
              └─ транзиентный отказ → ROLLBACK TO SAVEPOINT, pending_result остаётся → 200
                                      (media_webhook_outcome=completion_deferred)
        ↓ commit
```

**`_advance` proxy-задачи** (клиентский `GET` и согласователь, под тем же `FOR UPDATE`):

```
  ├─ pending_result непуст → ОБЩИЙ ПУТЬ ЗАВЕРШЕНИЯ; исключение → _close_if_overdue
  │     (moderation_unavailable | internal_error) либо, у молодой задачи, наверх — как сегодня
  ├─ возраст > MEDIA_JOB_DEADLINE_SECONDS → media_generation_deadline_exceeded
  │     (lastObservation = webhook_pending) → _fail("generation did not complete in time")
  └─ иначе → mark_running   (исходящих вызовов нет)
```

Диаграммы выше — **полный** порядок: общий путь завершения — это сегодняшняя ветка `COMPLETED` §Поток опроса без изменения шагов; сервис для вебхука собирается `deps.build_media_generation_service` ([ADR-105 §B6](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)). **Контраст транзакционных границ (обе стороны помечены):** в вебхуке общий путь идёт под `SAVEPOINT`, и его отказ НЕ откатывает `pending_result`; в `_advance` отказ у задачи моложе дедлайна откатывает запрос целиком, как сегодня. Согласователь берёт незавершённые строки `provider <> '' ∨ fal_configured ∨ created_at < now − MEDIA_JOB_DEADLINE_SECONDS`, старейшие первыми; proxy-строки — через `FOR UPDATE SKIP LOCKED` (занятая вебхуком или `GET` строка пропускается до следующего тика — иначе захват, живущий до коммита пакета, и `UPDATE wallets` возврата дают взаимоблокировку с вебхуком); клиентский `GET` и вебхук ждут обычный `FOR UPDATE` ([ADR-108 §6](../../adr/ADR-108-media-generation-via-proxy.md)).

## Своё хранение результатов (ADR-109)

Норма — [ADR-109](../../adr/ADR-109-media-asset-local-storage-30d.md) (**код, миграция и инфраструктура в `main` (`f90d871`), выкачены (CI `36015691825` на `48f9018`, джоб `ssh deploy` — `success`)**); здесь — полный порядок шагов для точки чтения реализации. Хранение выключено (`MEDIA_ASSET_STORAGE_DIR` пуст) → ничего из этого раздела не выполняется, поведение модуля бит-в-бит прежнее.

**Путь завершения (опрос и вебхук) — одно присваивание, шагов не добавляется:**

```
общий путь завершения (ADR-108 §5, шаги и порядок прежние)
  └─ mark_completed(result)  при включённом хранении и непустых assets:
        asset_store_status := 'pending'
        assets_expire_at   := now() + MEDIA_ASSET_RETENTION_DAYS × 86400 s   (один раз, не сдвигается)
  ↓ коммит терминала — сохранение НИКОГДА не откатывает и не откладывает завершение
```

**Фоновый цикл «хранилище ассетов»** (lifespan, период `MEDIA_ASSET_STORE_INTERVAL_SECONDS`; исполняет ОДИН воркер инстанса за раз; ни одной открытой транзакции во время сети и записи файла):

```
тик (в КАЖДОМ воркере)
  ├─ Gauge (ДО права исполнения, ADR-109 §9): media_asset_storage_free_bytes,
  │     media_asset_store_pending, media_asset_store_failed, media_asset_missing,
  │     media_asset_cleanup_blocked — из ФС и БД (free_bytes при провале измерения
  │     снимается, а не ставится в 0)
  ├─ право исполнения не получено (другой воркер исполняет) → конец тика
  ├─ корень недоступен (нет каталога / не пишется / нет маркера .media-assets-root)
  │                                            → deferred_unavailable (строки не трогаются)
  ├─ короткая транзакция: кандидаты status='completed' ∧ asset_store_status='pending'
  │     ∧ (next_attempt_at IS NULL ∨ ≤ now), старейшие первыми, пакет 5
  ├─ для каждого (вне транзакции):
  │     ├─ assets_expire_at прошёл           → expired (без скачивания)
  │     ├─ БЕЗ СЕТИ, до любого запроса:
  │     │     ├─ хост вне FAL_UPLOAD_HOST_SUFFIXES ∪ MEDIA_RESULT_HOST_SUFFIXES / не https
  │     │     │                                → failed (host_rejected)
  │     │     └─ свободно − MEDIA_ASSET_MAX_BYTES < MEDIA_ASSET_MIN_FREE_BYTES
  │     │                                      → deferred_low_disk (next_attempt_at = now + 300 s)
  │     ├─ GET https, без redirect, предел MEDIA_ASSET_MAX_BYTES
  │     │     ├─ 404/410            → failed (gone)
  │     │     ├─ больше предела     → failed (too_large), частичный файл удалён
  │     │     ├─ свободно − Content-Length < MIN_FREE → deferred_low_disk, чтение прервано
  │     │     ├─ таймаут/5xx/IO     → attempts + 1 < 12 → retry (пауза min(2^attempts·30 s, 3600 s))
  │     │     │                       attempts + 1 = 12 → failed (exhausted)
  │     │     └─ ok → tmp в каталоге назначения → fsync → rename в <index>
  │     └─ все ассеты задачи записаны → короткая транзакция:
  │           UPDATE … SET stored, assets_stored_at, assets_stored_bytes, stored_assets
  │           WHERE asset_store_status='pending'   (строку удалили → 0 строк, файлы — сироты §6.3)
  └─ не чаще раза в 3600 s — очистка:
        ├─ assets_expire_at < now ∧ status ∈ {stored,pending,failed,missing}
        │     → rm -r <dir>/<shard>/<jobId> (ENOENT — не ошибка) → затем expired, stored_assets := NULL
        ├─ tmp-файлы старше 3600 s → удалить
        └─ только если pg_system_identifier маркера = system_identifier текущей базы:
              <jobId> без строки media_jobs и mtime старше 3600 s → удалить (DELETE / каскад users);
           иначе → пропускается ТОЛЬКО этот шаг (media_asset_cleanup_aborted,
              media_asset_cleanup_blocked = 1; выход — оператор переписывает маркер)
   Вне сервера с работающим api и включённым хранением цикла нет: откат чистит каталог
   тем же шагом, вернувшийся сервер — при пересборке (ADR-109 §6, Q-109-7).
```

**Download-роут:**

```
GET|HEAD /v1/media/jobs/{jobId}/assets/{index}/{token}
  ├─ нет строки / нет index         → 404        ┐ как сегодня
  ├─ токен невалиден                → 401        ┘
  ├─ хранение выключено             → диск не читается → путь источника          source=remote
  ├─ корень недоступен (нет каталога / не пишется / нет маркера .media-assets-root)
  │     → WARNING media_asset_storage_unavailable, статус НЕ меняется → источник  source=remote
  ├─ status ∈ {stored, missing} ∧ now < assets_expire_at ∧ файл читается
  │     (missing → UPDATE … SET 'stored' — переход обратим)
  │     → байты с диска (Range/If-Range → 206, HEAD, тот же набор заголовков;
  │       невыполнимый Range → 404, 304 не отдаётся)                             source=local
  ├─ stored ∧ в сроке ∧ файла НЕТ   → WARNING media_asset_local_missing,
  │     UPDATE … SET 'missing' WHERE status='stored' → путь источника           source=local_missing
  ├─ иная ошибка чтения файла       → WARNING, статус не меняется → путь источника source=remote
  └─ иначе                          → stream_fal_asset как сегодня               source=remote
```

Диаграммы выше — **полный** порядок. **Контраст (обе стороны помечены):** пост-модерация и completion handler стоят ВНУТРИ общего пути завершения и вправе его отложить ([ADR-108 §5](../../adr/ADR-108-media-generation-via-proxy.md)); сохранение стоит СНАРУЖИ, после коммита, и ни отложить, ни откатить завершение не может — переносить скачивание в общий путь ЗАПРЕЩЕНО ([ADR-109 §2](../../adr/ADR-109-media-asset-local-storage-30d.md)). Провал сохранения кредитов не возвращает ([ADR-109 §8](../../adr/ADR-109-media-asset-local-storage-30d.md)). Вход правки по `sourceJobId` по-прежнему URL провайдера ([Q-109-5](../../99-open-questions.md)).
