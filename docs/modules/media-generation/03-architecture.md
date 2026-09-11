# 03 — Архитектура

## Файлы

| Файл | Роль |
|---|---|
| `src/app/media_generation/catalog.py` | реестр моделей: публичный id → endpoint fal, allowlist входных полей, имя поля с картинкой, цена по умолчанию |
| `src/app/media_generation/fal_client.py` | исходящий httpx-клиент fal: `submit` / `status` / `result` очереди и `upload` хранилища, маппинг ошибок |
| `src/app/media_generation/repository.py` | персистентность `media_jobs`, все запросы в скоупе владельца |
| `src/app/media_generation/service.py` | use-cases: `submit` (модерация входа → цена → списание → сабмит → задача), `get_job` (опрос + переходы + пост-модерация + возврат), `list_jobs` (лента), `delete_job`, `upload_reference_image` |
| `src/app/moderation/service.py` | клиент модерации UGC ([ADR-086](../../adr/ADR-086-ugc-moderation.md)): один вызов `omni-moderation-latest` на текст+изображения, вычисление вердикта, метрики/лог. Общий для media, chat и uploads |
| `src/app/media_generation/cursor.py` | непрозрачный keyset-курсор ленты `(created_at, id)` |
| `src/app/schemas/media.py` | схемы запросов/ответов (camelCase, `extra=forbid`) |
| `src/app/api_gateway/routers/media.py` | роутер `/v1/media/*`, rate limit, проекция в схемы ответа |
| `migrations/versions/20260804_0018_media_jobs.py` | миграция таблицы |
| `migrations/versions/20260805_0019_media_jobs_edit_chain.py` | цепочка правок: `parent_job_id`, `input_image_urls` |

Wiring — `deps.get_media_generation_service` / `deps.get_fal_client`.

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
       ├─ FAILED / CANCELED → wallet.grant(key=media-refund:{jobId}) → mark_failed
       ├─ IN_QUEUE / IN_PROGRESS → mark_running          ┐ опрос НЕ дал конечного
       └─ любое иное исключение (5xx, 429, 401/403,      │ состояния:
          таймаут, обрыв, битый JSON — на status или     │ ДЕДЛАЙН (ADR-105 §B2)
          на result; недоступна пост-модерация)          ┘
             ├─ now − created_at > MEDIA_JOB_DEADLINE_SECONDS
             │     → media_generation_deadline_exceeded → wallet.grant(key=media-refund:{jobId})
             │       → mark_failed(error="generation did not complete in time") → 200 failed
             └─ иначе → как было: mark_running / исключение наверх (клиенту 502/503/429,
                        согласователю — media_reconcile_job_error), следующий опрос повторит
```

Диаграмма выше — **полный** порядок опроса, включая шаг пост-модерации ([ADR-086](../../adr/ADR-086-ugc-moderation.md)), терминальные `422`/`404` на опросе (факт кода: `MediaGenerationService._advance` ловит `ValidationFailedError` и `UpstreamJobGoneError`; `404` — коммит `5ebf963`, в [ADR-060 §3](../../adr/ADR-060-media-generation-fal.md) не значился) и ветку дедлайна **[ADR-105 §B2](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md) (норма; на `f8f4b37` НЕ реализована — сегодня нижняя ветка без дедлайна, и задача, на которую fal не даёт конечного ответа, опрашивается вечно без возврата кредитов)**.

**Дедлайн задачи ([ADR-105 §B](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)).** Любая строка `media_jobs` достигает `completed`/`failed` не позже `created_at + MEDIA_JOB_DEADLINE_SECONDS` (дефолт `21600`, 6 ч). Предикат двусторонний: **(а)** задача старше дедлайна, опрос не дал конечного состояния по ЛЮБОЙ причине ⇒ `failed` + возврат; **(б)** задача моложе дедлайна правилом не трогается, какой бы ни была ошибка, а задача старше дедлайна, чей опрос дал `COMPLETED` с ассетами или `FAILED`, получает этот исход, а не текст дедлайна (опрос выполняется всегда — «последний шанс»). Мерило — возраст, а не число попыток: частота опроса зависит от клиента, интервала и числа реплик. Значение `<= 0` приводится к дефолту.

**Заблокированный результат не сохраняет ассеты.** `media_jobs.result` пишется как `{"assets": []}` — иначе файл остался бы достижим по signed-URL download-роуту ([ADR-085](../../adr/ADR-085-media-asset-download-proxy.md)), и блокировка была бы декоративной. У `kind=video` пост-модерации нет (провайдер модерации не принимает видео) — вердикт видео-задачи отражает только вход, `stage: "input"` ([Q-086-2](../../99-open-questions.md)).

**Недоступность провайдера модерации на опросе** ведёт себя как транзиентная ошибка апстрима: задача остаётся non-terminal, `mark_completed` не выполняется, следующий опрос (или reconciler, [ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)) доберёт исход — **но не позже дедлайна задачи**: по его истечении `failed` с возвратом, ассеты не выдаются ([ADR-105 §B2](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md), `lastObservation = moderation_unavailable`). Отдавать ассеты «пока модерация недоступна» запрещено — это и есть fail-open, отвергнутый в [ADR-086 §7](../../adr/ADR-086-ugc-moderation.md); при `MODERATION_FAIL_OPEN=true` (аварийный режим оператора) задача завершается с `moderation.status = "unchecked"`.

`COMPLETED` без пригодного URL трактуется как провал: с точки зрения пользователя разницы между «упало» и «завершилось без результата» нет, а кредиты в обоих случаях должны вернуться.

Возврат идемпотентен дважды: ключом ledger `media-refund:{jobId}` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)) и флагом `credits_refunded` в строке — флаг лишь избавляет от повторного вызова, гарантию даёт ключ.

`GET /v1/media/jobs` (лента) провайдера **не опрашивает**: N задач не должны разворачиваться в N исходящих вызовов. Пагинация keyset-курсорная по `(created_at, id)`: лента растёт с головы, и при `offset` вставка новой задачи между запросами дала бы дубли и пропуски.

`DELETE /v1/media/jobs/{jobId}` удаляет только нашу строку и только у терминальной задачи: возврат кредитов привязан к строке и срабатывает при опросе, поэтому удаление незавершённой уничтожило бы единственное место, где этот возврат может произойти ([ADR-063 §4](../../adr/ADR-063-media-feed-edit-chains-and-job-deletion.md)).

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
| `media_reconcile_job_error` | продвижение задачи согласователем завершилось исключением (WARNING): `jobId`; **+ `exceptionClass`** — имя класса исключения ([ADR-105 §B5](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md), норма; на `f8f4b37` поля нет) |
| `media_generation_deadline_exceeded` | **норма [ADR-105 §B5](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md), на `f8f4b37` не реализовано.** Задача доведена до `failed` по дедлайну (WARNING, пишется ветка дедлайна `_advance` перед `_fail`): `jobId`, `model`, `ageSeconds`, `lastObservation` ∈ `upstream_error` \| `upstream_pending` \| `moderation_unavailable` \| `not_configured` \| `internal_error` (предикаты — в ADR), `upstreamStatus` (только когда исключение fal его несёт). Следом — `media_generation_failed` |

## Согласователь: одна сборка сервиса ([ADR-105 §B6](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md))

`reconcile_once` (`src/app/media_generation/reconciler.py`) собирает `MediaGenerationService` **той же функцией и с тем же набором зависимостей**, что `deps.get_media_generation_service` (`repo`, `fal`, `wallet`, `settings`, `push`, `request_logs`, `moderation`). ⚠️ Факт кода на `f8f4b37`: согласователь собирает сервис сам и **без** `request_logs` и `moderation` — строка `request_logs` задачи, доведённой согласователем, остаётся `queued` ([ADR-077 §3](../../adr/ADR-077-crm-request-logs.md) не выполняется), а картинка, готовность которой обнаружил согласователь, выдаётся **без пост-модерации** ([ADR-086 §5](../../adr/ADR-086-ugc-moderation.md) не выполняется). При пустом `FAL_API_KEY` согласователь не опрашивает, но задачи старше дедлайна доводит до `failed` (`lastObservation = not_configured`, [ADR-105 §B7](../../adr/ADR-105-provider-failure-input-shape-and-media-deadline.md)); сегодня `reconcile_once` при пустом ключе возвращает `0` до выборки. Выборка `MediaJobsRepository.list_non_terminal` — по-прежнему старейшие первыми; дедлайн ограничивает сверху, сколько в голове пачки может лежать мёртвых задач.
