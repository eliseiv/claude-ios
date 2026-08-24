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

Списание, сабмит и вставка живут в одной request-транзакции (`session_scope` коммитит один раз в конце). Отсюда два инварианта:

- **сабмит упал → списание откатилось**: пользователь не платит за запуск, который провайдер не принял;
- **строка `media_jobs` существует ⇒ за неё заплачено и провайдер ею владеет**.

**Инвариант модерации входа ([ADR-086 §4](../../adr/ADR-086-ugc-moderation.md)):** проверка стоит **до** `wallet.consume` — иначе пользователь платит за контент, который заведомо будет отклонён. **Контраст (обе стороны помечены):** пост-модерация результата (см. поток опроса ниже) идёт **после** списания по построению — раньше вердикта о выходе не существует — и потому **обязана вернуть кредиты**. Правило «до списания» на неё не переносится; правило «вернуть кредиты» на пре-модерацию не переносится (там списания ещё не было).

Порядок «сначала списать, потом сабмитить» выбран намеренно: обратный порядок оставлял бы оплаченные запуски без строки при отказе БД. При текущем порядке худший случай — осиротевший запуск у провайдера, за который пользователь не заплатил.

## Поток опроса

```
GET /v1/media/jobs/{jobId}
  ├─ repo.get(job_id, user_id)          → 404 (чужая/нет — неотличимо)
  ├─ status ∈ {completed, failed}?      → ответ из БД, провайдер не дёргается
  └─ fal.status(status_url)
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
       └─ IN_QUEUE / IN_PROGRESS → mark_running
```

Диаграмма выше — **полный** порядок опроса, включая шаг пост-модерации ([ADR-086](../../adr/ADR-086-ugc-moderation.md)).

**Заблокированный результат не сохраняет ассеты.** `media_jobs.result` пишется как `{"assets": []}` — иначе файл остался бы достижим по signed-URL download-роуту ([ADR-085](../../adr/ADR-085-media-asset-download-proxy.md)), и блокировка была бы декоративной. У `kind=video` пост-модерации нет (провайдер модерации не принимает видео) — вердикт видео-задачи отражает только вход, `stage: "input"` ([Q-086-2](../../99-open-questions.md)).

**Недоступность провайдера модерации на опросе** ведёт себя как транзиентная ошибка апстрима: задача остаётся non-terminal, `mark_completed` не выполняется, следующий опрос (или reconciler, [ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)) доберёт исход. Отдавать ассеты «пока модерация недоступна» запрещено — это и есть fail-open, отвергнутый в [ADR-086 §7](../../adr/ADR-086-ugc-moderation.md); при `MODERATION_FAIL_OPEN=true` (аварийный режим оператора) задача завершается с `moderation.status = "unchecked"`.

`COMPLETED` без пригодного URL трактуется как провал: с точки зрения пользователя разницы между «упало» и «завершилось без результата» нет, а кредиты в обоих случаях должны вернуться.

Возврат идемпотентен дважды: ключом ledger `media-refund:{jobId}` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)) и флагом `credits_refunded` в строке — флаг лишь избавляет от повторного вызова, гарантию даёт ключ.

`GET /v1/media/jobs` (лента) провайдера **не опрашивает**: N задач не должны разворачиваться в N исходящих вызовов. Пагинация keyset-курсорная по `(created_at, id)`: лента растёт с головы, и при `offset` вставка новой задачи между запросами дала бы дубли и пропуски.

`DELETE /v1/media/jobs/{jobId}` удаляет только нашу строку и только у терминальной задачи: возврат кредитов привязан к строке и срабатывает при опросе, поэтому удаление незавершённой уничтожило бы единственное место, где этот возврат может произойти ([ADR-063 §4](../../adr/ADR-063-media-feed-edit-chains-and-job-deletion.md)).

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
| `fal_call_outcome` | ошибка исходящего вызова: `reason`, `falEndpoint`, `upstreamStatus` |
