"""Media generation routes: /v1/media/* (media-generation/02-api-contracts.md, ADR-060).

JWT-protected (CurrentUser), owner-scoped: a foreign or missing job is 404. Generation is
asynchronous — the POST routes return a `queued` job and the client polls
`GET /v1/media/jobs/{jobId}`, which is the only route that touches the provider. Per-user rate
limit like the other non-chat endpoints.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_media_generation_service
from app.errors import RateLimitedError
from app.media_generation.catalog import KIND_IMAGE, KIND_VIDEO, all_models
from app.media_generation.service import MediaGenerationService, MediaJobView
from app.schemas.media import (
    ImageGenerationRequest,
    MediaAssetSchema,
    MediaJobResponse,
    MediaJobsListResponse,
    MediaModelSchema,
    MediaModelsResponse,
    MediaModeSchema,
    VideoGenerationRequest,
)

router = APIRouter(prefix="/v1/media", tags=["Media"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


def _job_response(view: MediaJobView) -> MediaJobResponse:
    job = view.job
    return MediaJobResponse(
        jobId=job.id,
        status=job.status,
        kind=job.kind,
        model=job.model_id,
        prompt=job.prompt,
        creditsCharged=job.credits_charged,
        creditsRefunded=job.credits_refunded,
        assets=[
            MediaAssetSchema(url=a.url, contentType=a.content_type, fileName=a.file_name)
            for a in view.assets
        ],
        error=job.error,
        createdAt=job.created_at,
        updatedAt=job.updated_at,
    )


@router.get(
    "/models",
    response_model=MediaModelsResponse,
    summary="Каталог моделей генерации",
    description=(
        "Возвращает доступные модели генерации фото и видео: идентификатор для поля `model`, "
        "базовую цену в кредитах и **режимы** генерации. Режимов у модели два — без референсного "
        "изображения (`textToImage`/`textToVideo`) и с ним (`imageToImage`/`imageToVideo`); режим "
        "выбирается автоматически по наличию `imageUrls`/`imageUrl` в запросе. У каждого режима "
        "свои `params` (какие параметры он принимает) и свои наборы `aspectRatio`/`resolution`/"
        "`duration` — они действительно различаются между режимами. Пустой список означает, что "
        "параметр в этом режиме не поддерживается: присланное значение вернёт `422` до списания. "
        "Строьте контролы UI по режиму, а не по модели."
    ),
)
async def list_media_models(
    request: Request,
    current: CurrentUser,
    media: Annotated[MediaGenerationService, Depends(get_media_generation_service)],
) -> MediaModelsResponse:
    await _rate_limit(current.user_id)
    return MediaModelsResponse(
        models=[
            MediaModelSchema(
                id=model.id,
                title=model.title,
                kind=model.kind,
                credits=media.credits_for(model),
                baseDurationSeconds=model.base_duration_seconds,
                supportsImageInput=model.image_variant is not None,
                maxInputImages=model.max_input_images if model.image_variant else 0,
                supportsAudio=model.supports_audio,
                modes=[
                    MediaModeSchema(
                        mode=mode,
                        # Sorted so the payload is stable across restarts (fields is a frozenset).
                        params=sorted(variant.fields),
                        aspectRatios=list(variant.aspect_ratios),
                        resolutions=list(variant.resolutions),
                        durations=list(variant.durations),
                    )
                    for mode, variant in model.variants()
                ],
            )
            for model in all_models()
        ]
    )


@router.post(
    "/images",
    response_model=MediaJobResponse,
    status_code=202,
    summary="Сгенерировать изображение",
    description=(
        "Ставит генерацию изображения в очередь и списывает кредиты по цене модели (цена берётся "
        "с сервера, поле в запросе не предусмотрено): `credits × numImages`. Отвечает `202` с "
        "задачей в статусе `queued` — результат забирайте через `GET /v1/media/jobs/{jobId}`. "
        "Непустой `imageUrls` включает режим редактирования референсных изображений. Параметры "
        "`aspectRatio`/`resolution`/`numImages`/`outputFormat`/`seed` проверяются по набору "
        "выбранной модели **до** списания — недопустимое значение вернёт `422` бесплатно. "
        "Недостаточно кредитов — `409 insufficient_credits` (списания не будет); генерация не "
        "настроена на инстансе — `503 media_generation_not_configured`."
    ),
)
async def generate_image(
    body: ImageGenerationRequest,
    request: Request,
    current: CurrentUser,
    media: Annotated[MediaGenerationService, Depends(get_media_generation_service)],
) -> MediaJobResponse:
    await _rate_limit(current.user_id)
    view = await media.submit(
        user_id=current.user_id,
        kind=KIND_IMAGE,
        model_id=body.model,
        prompt=body.prompt,
        image_urls=list(body.imageUrls or []),
        params={
            "aspectRatio": body.aspectRatio,
            "resolution": body.resolution,
            "numImages": body.numImages,
            "outputFormat": body.outputFormat,
            "seed": body.seed,
        },
    )
    return _job_response(view)


@router.post(
    "/videos",
    response_model=MediaJobResponse,
    status_code=202,
    summary="Сгенерировать видео",
    description=(
        "Ставит генерацию видео в очередь и списывает кредиты по цене модели, масштабированной "
        "длительностью: `credits × ceil(duration / baseDurationSeconds)`. Отвечает `202` с "
        "задачей в статусе `queued`: видео генерируется минутами, поэтому результат забирается "
        "опросом `GET /v1/media/jobs/{jobId}`. Заданный `imageUrl` включает режим image-to-video "
        "(стартовый кадр) — в нём `aspectRatio` берётся из кадра и моделями Kling не принимается. "
        "Прочие параметры: `duration`, `resolution`, `negativePrompt`, `generateAudio` (у моделей "
        "с `supportsAudio`), `cfgScale` (Kling), `seed`; допустимые значения — в `modes` каталога, "
        "проверяются до списания. Если генерация упадёт на стороне провайдера, задача перейдёт в "
        "`failed`, а списанные кредиты вернутся на баланс."
    ),
)
async def generate_video(
    body: VideoGenerationRequest,
    request: Request,
    current: CurrentUser,
    media: Annotated[MediaGenerationService, Depends(get_media_generation_service)],
) -> MediaJobResponse:
    await _rate_limit(current.user_id)
    view = await media.submit(
        user_id=current.user_id,
        kind=KIND_VIDEO,
        model_id=body.model,
        prompt=body.prompt,
        image_urls=[body.imageUrl] if body.imageUrl else [],
        params={
            "negativePrompt": body.negativePrompt,
            "aspectRatio": body.aspectRatio,
            "resolution": body.resolution,
            "duration": body.duration,
            "generateAudio": body.generateAudio,
            "cfgScale": body.cfgScale,
            "seed": body.seed,
        },
    )
    return _job_response(view)


@router.get(
    "/jobs",
    response_model=MediaJobsListResponse,
    summary="Список задач генерации",
    description=(
        "Задачи генерации пользователя, новые сверху. Только чтение: у незавершённых задач "
        "отдаётся последнее известное состояние без обращения к провайдеру — обновляйте нужную "
        "задачу через `GET /v1/media/jobs/{jobId}`. `kind` фильтрует по типу генерации."
    ),
)
async def list_media_jobs(
    request: Request,
    current: CurrentUser,
    media: Annotated[MediaGenerationService, Depends(get_media_generation_service)],
    limit: Annotated[
        int, Query(ge=1, le=100, description="Сколько задач вернуть (1–100, по умолчанию 20).")
    ] = 20,
    kind: Annotated[
        Literal["image", "video"] | None,
        Query(description="Фильтр по типу генерации. Опущен — задачи обоих типов."),
    ] = None,
) -> MediaJobsListResponse:
    await _rate_limit(current.user_id)
    views = await media.list_jobs(user_id=current.user_id, limit=limit, kind=kind)
    return MediaJobsListResponse(jobs=[_job_response(view) for view in views])


@router.get(
    "/jobs/{job_id}",
    response_model=MediaJobResponse,
    summary="Состояние задачи генерации",
    description=(
        "Актуальное состояние задачи. Пока задача не завершена, эндпоинт опрашивает провайдера и "
        "обновляет статус; после `completed`/`failed` отвечает из базы. При `completed` в `assets` "
        "лежат ссылки на результат, при `failed` — причина в `error`, а кредиты уже возвращены. "
        "Чужая или несуществующая задача — `404`."
    ),
)
async def get_media_job(
    request: Request,
    current: CurrentUser,
    media: Annotated[MediaGenerationService, Depends(get_media_generation_service)],
    job_id: Annotated[uuid.UUID, Path(description="Идентификатор задачи генерации.")],
) -> MediaJobResponse:
    await _rate_limit(current.user_id)
    view = await media.get_job(user_id=current.user_id, job_id=job_id)
    return _job_response(view)
