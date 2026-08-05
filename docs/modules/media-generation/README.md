# Module: Media Generation (фото и видео через fal.ai)

- Статус: **Реализован**; цены откалиброваны под прайс fal от 2026-08-05 с двукратным покрытием закупки ([ADR-061](../../adr/ADR-061-fal-price-calibration-and-priced-defaults.md), закрывает [Q-060-3](../../99-open-questions.md)). Периодическая сверка прайса — [Q-061-1](../../99-open-questions.md).
- Ответственность: генерация изображений и видео на провайдере [fal.ai](https://fal.ai) по асинхронному контракту «поставить задачу → опросить результат» ([ADR-060](../../adr/ADR-060-media-generation-fal.md)). Списание кредитов при постановке, возврат при провале у провайдера.
- Модели MVP: **Nano Banana Pro**, **Nano Banana 2** (изображения), **Kling Video**, **Kling Video V3**, **Veo 3.1** (видео).
- Активируется **по инстансу**: `FAL_API_KEY` не задан → вся поверхность `/v1/media/*` отвечает `503 media_generation_not_configured`.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [04-data-model.md](04-data-model.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Генерация медиа идёт **мимо** chat-оркестратора: это не инструмент tool-loop, шагов в `chat_steps` не создаётся, от `LLM_PROVIDER` ([ADR-033](../../adr/ADR-033-llm-provider-abstraction.md)) не зависит. Кошелёк и ledger переиспользуются как есть ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)).

## DoD (выполнено)
- ✅ `GET /v1/media/models` — каталог моделей: id, тип, базовая цена в кредитах, поддержка референсных изображений и звука, **режимы** (`textToImage`/`imageToImage`/`textToVideo`/`imageToVideo`) с их параметрами и допустимыми значениями `aspectRatio`/`resolution`/`duration`.
- ✅ `POST /v1/media/images`, `POST /v1/media/videos` — постановка в очередь fal, ответ `202` с задачей в статусе `queued`; кредиты списаны по серверной цене (поля цены в теле нет — anti-tamper).
- ✅ Оба режима в каждом маршруте: text-to-image / image-to-image и text-to-video / image-to-video — переключаются наличием `imageUrls`/`imageUrl`, endpoint провайдера выбирает сервер.
- ✅ Параметры генерации: `aspectRatio`, `resolution`, `duration`, `numImages`, `outputFormat`, `negativePrompt`, `generateAudio`, `cfgScale`, `seed` — каждый валидируется против набора **режима** до списания.
- ✅ Цена масштабируется объёмом выпуска: `× numImages` у изображений, `× ceil(duration / baseDurationSeconds)` у видео; баланс проверяется по итоговой цене.
- ✅ `GET /v1/media/jobs/{jobId}` — опрос провайдера, пока задача не терминальна; `completed` → `assets`, `failed` → `error` + возврат кредитов (идемпотентный).
- ✅ `GET /v1/media/jobs` — листинг владельца newest-first, read-only (провайдер не опрашивается), фильтр `kind`.
- ✅ Реестр моделей на сервере (`media_generation/catalog.py`): публичный id → endpoint fal, allowlist входных полей и наборы значений per-variant, имя поля с референсным изображением per-family.
- ✅ Списание и создание задачи — в одной транзакции: сабмит упал → списание откатилось.
- ✅ Миграция `0018` (`media_jobs`), single head. Маппинг ошибок провайдера в `502`/`503`/`422`/`429` без утечки upstream-тела.
- ✅ Отдельный блок `Media` в Swagger с примерами запросов на оба режима.

## Changelog
- 2026-08-04: модуль создан и реализован — [ADR-060](../../adr/ADR-060-media-generation-fal.md). Backend: `src/app/media_generation/{catalog,fal_client,repository,service}.py`, `src/app/schemas/media.py`, `src/app/api_gateway/routers/media.py`, миграция `0018_media_jobs`, config `FAL_*`/`MEDIA_*`, ошибка `media_generation_not_configured`.
- 2026-08-04: реестр сверен с опубликованными схемами fal; наборы значений перенесены на уровень режима (у Veo `aspectRatio: "auto"` только с картинкой), исправлены `0.5K` у Nano Banana 2, `4k` у Veo и `3`…`15` у Kling V3; добавлены `cfgScale`, `seed`, `generateAudio` у Kling V3, `negativePrompt` у Veo; цена масштабируется по `numImages`/`duration`.
