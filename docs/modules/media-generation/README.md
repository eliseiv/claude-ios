# Module: Media Generation (фото и видео через fal.ai)

- Статус: **Реализован**; цены откалиброваны под прайс fal от 2026-08-05 с двукратным покрытием закупки ([ADR-061](../../adr/ADR-061-fal-price-calibration-and-priced-defaults.md), закрывает [Q-060-3](../../99-open-questions.md)). Периодическая сверка прайса — [Q-061-1](../../99-open-questions.md).
- Ответственность: генерация изображений и видео на провайдере [fal.ai](https://fal.ai) по асинхронному контракту «поставить задачу → опросить результат» ([ADR-060](../../adr/ADR-060-media-generation-fal.md)). Списание кредитов при постановке, возврат при провале у провайдера.
- Модели MVP: **Nano Banana Pro**, **Nano Banana 2** (изображения), **Kling Video**, **Kling Video V3**, **Veo 3.1** (видео).
- Активируется **по инстансу**: `FAL_API_KEY` не задан → постановка/опрос/uploads/`GET /v1/media/models` отвечают `503 media_generation_not_configured`. Каталог шаблонов (`/v1/media/templates/*`) от fal не зависит ([ADR-066](../../adr/ADR-066-media-templates-catalog.md)).

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [04-data-model.md](04-data-model.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Байты результата и лента `/v1/media/*` живут в модуле media (не в `chat_steps`). Сабмит из чата — через global tools `media.generate_image` / `media.generate_video` ([ADR-068](../../adr/ADR-068-media-generate-chat-tools.md)): tool только ставит задачу и отдаёт `jobId` в `ChatResponse.mediaJobs`; ожидание fal в tool-loop запрещено. Пикер параметров в чате — `media.ask_params` → `ChatResponse.mediaChoices` / `mediaSelection` ([ADR-070](../../adr/ADR-070-media-choices-wizard.md)); options только из `catalog.py`. Контракт `/v1/media/*` без изменений. От `LLM_PROVIDER` ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) media не зависит. Кошелёк/ledger — [ADR-005](../../adr/ADR-005-idempotency-ledger.md).
>
> **Где iOS искать «историю генераций»:** (1) лента пользователя — `GET /v1/media/jobs` (+ cursor); (2) в конкретном чате — `GET /v1/chats/{id}` → `steps[].payload.mediaJobs` на assistant → `GET /v1/media/jobs/{jobId}`. Если `GET /v1/media/models` даёт `503 media_generation_not_configured` — на инстансе нет `FAL_API_KEY`. Chat-tools media (`media.ask_params` / `generate_*`) дополнительно гейтятся `CHAT_MEDIA_TOOLS_ENABLED` ([ADR-072](../../adr/ADR-072-chat-media-tools-instance-gate.md)): на `ravelumi`, `novirell`, `claude-ios` (broadnova), `corvionet`, `lunexoro` — `false` (REST media есть, генерация из чата выключена).

## DoD (выполнено)
- ✅ `GET /v1/media/models` — каталог моделей: id, тип, базовая цена в кредитах, поддержка референсных изображений и звука, **режимы** (`textToImage`/`imageToImage`/`textToVideo`/`imageToVideo`) с их параметрами и допустимыми значениями `aspectRatio`/`resolution`/`duration`.
- ✅ `POST /v1/media/uploads` — загрузка локального изображения (inline base64) в хранилище провайдера, ответ `201` с https-ссылкой для `imageUrls`/`imageUrl` ([ADR-062](../../adr/ADR-062-media-upload-via-fal-storage.md)). Кредитов не стоит.
- ✅ `POST /v1/media/images`, `POST /v1/media/videos` — постановка в очередь fal, ответ `202` с задачей в статусе `queued`; кредиты списаны по серверной цене (поля цены в теле нет — anti-tamper).
- ✅ Оба режима в каждом маршруте: text-to-image / image-to-image и text-to-video / image-to-video — переключаются наличием `imageUrls`/`imageUrl`, endpoint провайдера выбирает сервер.
- ✅ Параметры генерации: `aspectRatio`, `resolution`, `duration`, `numImages`, `outputFormat`, `negativePrompt`, `generateAudio`, `cfgScale`, `seed` — каждый валидируется против набора **режима** до списания.
- ✅ Цена масштабируется объёмом выпуска: `× numImages` у изображений, `× ceil(duration / baseDurationSeconds)` у видео; баланс проверяется по итоговой цене.
- ✅ `GET /v1/media/jobs/{jobId}` — опрос провайдера, пока задача не терминальна; `completed` → `assets` (signed URL на наш домен, [ADR-085](../../adr/ADR-085-media-asset-download-proxy.md)), `failed` → `error` + возврат кредитов (идемпотентный).
- ✅ `GET`/`HEAD /v1/media/jobs/{jobId}/assets/{index}/{token}` — стрим байтов с fal без JWT; в БД остаётся fal CDN.
- ✅ `GET /v1/media/jobs` — лента владельца newest-first, read-only (провайдер не опрашивается), фильтр `kind`, курсорная пагинация ([ADR-063](../../adr/ADR-063-media-feed-edit-chains-and-job-deletion.md)).
- ✅ `DELETE /v1/media/jobs/{jobId}` — убрать завершённую задачу из ленты; `409 job_not_terminal` на незавершённой (иначе некому вернуть кредиты при провале).
- ✅ Цепочки правок: `sourceJobId` в запросе генерации, `parentJobId`/`inputImageUrls` в объекте задачи.
- ✅ Реестр моделей на сервере (`media_generation/catalog.py`): публичный id → endpoint fal, allowlist входных полей и наборы значений per-variant, имя поля с референсным изображением per-family.
- ✅ Списание и создание задачи — в одной транзакции: сабмит упал → списание откатилось.
- ✅ Миграция `0018` (`media_jobs`), single head. Маппинг ошибок провайдера в `502`/`503`/`422`/`429` без утечки upstream-тела.
- ✅ Отдельный блок `Media` в Swagger с примерами запросов на оба режима.
- ✅ Каталог шаблонов галереи: `GET /v1/media/templates/images|videos`, публичный cover GET, admin POST/DELETE с base64-обложкой ([ADR-066](../../adr/ADR-066-media-templates-catalog.md)); seed 5+5; не зависит от `FAL_API_KEY`.

## Changelog
- 2026-08-24: **модерация UGC ([ADR-086](../../adr/ADR-086-ugc-moderation.md), docs-only — код не написан).** Пре-модерация `prompt` + клиентских референсов **до** `wallet.consume` (нарушение → `422 content_policy_violation`, кредиты не тронуты); пост-модерация результата **image**-генерации (нарушение → `failed`, `assets: []`, возврат кредитов тем же ключом `media-refund:{jobId}`, push не шлётся); у video пост-модерации нет ([Q-086-2](../../99-open-questions.md)). Новое поле ответа `moderation {status,stage,categories,checkedAt}` — и в задаче, и в ленте; `status` задачи остаётся закрытым набором из четырёх значений (новое значение НЕ вводится — сломало бы выпущенные клиенты). Колонка `media_jobs.moderation JSONB NULL`, expand-only миграция, без backfill (`NULL` → `unchecked`). Провайдер — OpenAI `omni-moderation-latest`, общий для media/chat/uploads, ключ `MODERATION_API_KEY` (фолбэк `OPENAI_API_KEY`; на anthropic-инстансах обязателен явно). Fail-closed: недоступность → `503 moderation_unavailable`, аварийный `MODERATION_FAIL_OPEN`. Отказ на пути chat-tools (`media.generate_*`) ход **не роняет** — tool-result error. Scope backend + qa + devops (env на 22 инстанса **до** мержа).
- 2026-08-21: прокси ассетов — signed URL вместо голого fal.media, `GET`/`HEAD …/assets/{index}/{token}` — [ADR-085](../../adr/ADR-085-media-asset-download-proxy.md); env `MEDIA_DOWNLOAD_TTL_SECONDS`.
- 2026-08-11: quiz-like пикер параметров в чате — `media.ask_params` + `mediaChoices`/`mediaSelection` — [ADR-070](../../adr/ADR-070-media-choices-wizard.md); `/v1/media/*` без изменений.
- 2026-08-11: chat tools `media.generate_image`/`media.generate_video` + `ChatResponse.mediaJobs` — [ADR-068](../../adr/ADR-068-media-generate-chat-tools.md); `/v1/media/*` без изменений.
- 2026-08-10: каталог шаблонов галереи — `GET /v1/media/templates/images|videos`, cover GET, admin POST/DELETE — [ADR-066](../../adr/ADR-066-media-templates-catalog.md); миграция `0021_media_templates`, seed 5+5.
- 2026-08-04: модуль создан и реализован — [ADR-060](../../adr/ADR-060-media-generation-fal.md). Backend: `src/app/media_generation/{catalog,fal_client,repository,service}.py`, `src/app/schemas/media.py`, `src/app/api_gateway/routers/media.py`, миграция `0018_media_jobs`, config `FAL_*`/`MEDIA_*`, ошибка `media_generation_not_configured`.
- 2026-08-04: реестр сверен с опубликованными схемами fal; наборы значений перенесены на уровень режима (у Veo `aspectRatio: "auto"` только с картинкой), исправлены `0.5K` у Nano Banana 2, `4k` у Veo и `3`…`15` у Kling V3; добавлены `cfgScale`, `seed`, `generateAudio` у Kling V3, `negativePrompt` у Veo; цена масштабируется по `numImages`/`duration`.
- 2026-08-05: цены откалиброваны под прайс fal, влияющие на цену параметры получили серверные дефолты — [ADR-061](../../adr/ADR-061-fal-price-calibration-and-priced-defaults.md); env `FAL_ASSET_RETENTION_SECONDS`.
- 2026-08-05: `POST /v1/media/uploads` — загрузка локального изображения в хранилище провайдера — [ADR-062](../../adr/ADR-062-media-upload-via-fal-storage.md); env `FAL_REST_BASE`/`FAL_UPLOAD_HOST_SUFFIXES`/`MEDIA_UPLOAD_MAX_BYTES`/`MEDIA_UPLOAD_REQUEST_BODY_LIMIT`.
- 2026-08-05: лента с курсорной пагинацией, цепочки правок (`sourceJobId` → `parentJobId`/`inputImageUrls`) и `DELETE /v1/media/jobs/{jobId}` — [ADR-063](../../adr/ADR-063-media-feed-edit-chains-and-job-deletion.md); миграция `0019_media_edit_chain`.
