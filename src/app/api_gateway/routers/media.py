"""Media generation routes: /v1/media/* (media-generation/02-api-contracts.md, ADR-060).

JWT-protected (CurrentUser), owner-scoped: a foreign or missing job is 404. Generation is
asynchronous — the POST routes return a `queued` job and the client polls
`GET /v1/media/jobs/{jobId}`, which is the only route that touches the provider. Per-user rate
limit like the other non-chat endpoints.

Routes on this router are gated on the instance being configured for generation: without
`FAL_API_KEY` they answer `503 media_generation_not_configured` — including `GET /v1/media/models`.
Gallery templates live on a separate router (ADR-066) and are not gated.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import (
    CurrentUser,
    get_media_generation_service,
    get_request_log_writer,
    require_media_generation_configured,
)
from app.errors import AppError, RateLimitedError, ValidationFailedError
from app.media_generation.catalog import (
    KIND_IMAGE,
    KIND_VIDEO,
    all_models,
    resolution_credits_for_api,
)
from app.media_generation.cursor import InvalidCursorError, MediaJobCursor
from app.media_generation.service import MediaGenerationService, MediaJobView
from app.request_logs.service import RequestLogWriter
from app.schemas.media import (
    ImageGenerationRequest,
    MediaAssetSchema,
    MediaJobDeleteResponse,
    MediaJobResponse,
    MediaJobsListResponse,
    MediaModelSchema,
    MediaModelsResponse,
    MediaModeSchema,
    MediaUploadRequest,
    MediaUploadResponse,
    VideoGenerationRequest,
)

router = APIRouter(
    prefix="/v1/media",
    tags=["Media"],
    dependencies=[Depends(require_media_generation_configured)],
)


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
        parentJobId=job.parent_job_id,
        inputImageUrls=list(job.input_image_urls or []),
        createdAt=job.created_at,
        updatedAt=job.updated_at,
    )


def _decode_cursor(value: str | None) -> MediaJobCursor | None:
    if value is None:
        return None
    try:
        return MediaJobCursor.decode(value)
    except InvalidCursorError as exc:
        raise ValidationFailedError("invalid cursor") from exc


@router.get(
    "/models",
    response_model=MediaModelsResponse,
    summary="Каталог моделей генерации",
    description=(
        "Возвращает доступные модели генерации фото и видео: идентификатор для поля `model`, "
        "базовую цену и **ступени качества**. Image: `resolutionCredits[resolution] × numImages`. "
        "Video: `credits × ceil(duration/baseDurationSeconds) × resolutionMultipliers[resolution] "
        "× (audioMultiplier при generateAudio)`, итог округляется вверх. Mode text/image на цену "
        "не влияет. Режимов у модели два — без референса и с ним; у каждого свои `params`, наборы "
        "значений и `defaults`. Пустой список = параметр не поддерживается (`422` до списания). "
        "Влияющий на цену параметр, который вы не пришлёте, сервер подставит из `defaults` — "
        "используйте их в своём расчёте, иначе он разойдётся с `creditsCharged`. Стройте UI по "
        "режиму."
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
                resolutionCredits=(
                    resolution_credits_for_api(model, base_credits=media.credits_for(model)) or None
                ),
                resolutionMultipliers=(
                    dict(model.resolution_multipliers) if model.resolution_multipliers else None
                ),
                audioMultiplier=model.audio_multiplier,
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
                        defaults=dict(variant.defaults),
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
        "с сервера): `resolutionCredits[resolution] × numImages`. Не присланные `resolution` и "
        "`numImages` подставляются из `defaults` режима и отправляются провайдеру явно, поэтому "
        "цена всегда описывает именно тот запуск, который будет выполнен. "
        "Отвечает `202` с "
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
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
) -> MediaJobResponse:
    await _rate_limit(current.user_id)
    log_id = await request_logs.start(
        user_id=current.user_id, endpoint=request.url.path, prompt=body.prompt
    )
    try:
        view = await media.submit(
            user_id=current.user_id,
            kind=KIND_IMAGE,
            model_id=body.model,
            prompt=body.prompt,
            image_urls=list(body.imageUrls or []),
            source_job_id=body.sourceJobId,
            params={
                "aspectRatio": body.aspectRatio,
                "resolution": body.resolution,
                "numImages": body.numImages,
                "outputFormat": body.outputFormat,
                "seed": body.seed,
            },
        )
    except BaseException as exc:
        await request_logs.fail(
            log_id, status_code=exc.status_code if isinstance(exc, AppError) else 500
        )
        raise
    await request_logs.queue_media(
        log_id,
        media_job_id=view.job.id,
        tokens_spent=view.job.credits_charged,
    )
    return _job_response(view)


@router.post(
    "/videos",
    response_model=MediaJobResponse,
    status_code=202,
    summary="Сгенерировать видео",
    description=(
        "Ставит генерацию видео в очередь и списывает кредиты по цене модели, масштабированной "
        "длительностью и качеством: `ceil(credits × ceil(duration / baseDurationSeconds) × "
        "resolutionMultipliers × audioMultiplier)`. Не присланные `duration`/`resolution`/"
        "`generateAudio` подставляются из `defaults` режима и отправляются провайдеру явно — "
        "звук по умолчанию **выключен**, включайте его осознанно: он умножает цену. Отвечает `202` "
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
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
) -> MediaJobResponse:
    await _rate_limit(current.user_id)
    log_id = await request_logs.start(
        user_id=current.user_id, endpoint=request.url.path, prompt=body.prompt
    )
    try:
        view = await media.submit(
            user_id=current.user_id,
            kind=KIND_VIDEO,
            model_id=body.model,
            prompt=body.prompt,
            image_urls=[body.imageUrl] if body.imageUrl else [],
            source_job_id=body.sourceJobId,
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
    except BaseException as exc:
        await request_logs.fail(
            log_id, status_code=exc.status_code if isinstance(exc, AppError) else 500
        )
        raise
    await request_logs.queue_media(
        log_id,
        media_job_id=view.job.id,
        tokens_spent=view.job.credits_charged,
    )
    return _job_response(view)


@router.post(
    "/uploads",
    response_model=MediaUploadResponse,
    status_code=201,
    summary="Загрузить изображение для генерации по референсу",
    description=(
        "Принимает изображение в base64 и возвращает https-ссылку на него. Ссылку подставляйте в "
        "`imageUrls` (`POST /v1/media/images`, режим редактирования) или в `imageUrl` "
        "(`POST /v1/media/videos`, image-to-video) — оба поля принимают только https-URL, потому "
        "что файл скачивает сам провайдер. Кредитов не стоит. Допустимые типы: `image/jpeg`, "
        "`image/png`, `image/gif`, `image/webp`; тип сверяется с реальной сигнатурой файла. "
        "Файл больше допустимого размера — `413 payload_too_large`; провайдер недоступен — `502`; "
        "генерация не настроена на инстансе — `503`. Срок жизни ссылки — в `expiresAt` (`null` — "
        "срок не ограничен либо задан политикой провайдера, не полагайтесь на бессрочность)."
    ),
)
async def upload_media_file(
    body: MediaUploadRequest,
    request: Request,
    current: CurrentUser,
    media: Annotated[MediaGenerationService, Depends(get_media_generation_service)],
) -> MediaUploadResponse:
    await _rate_limit(current.user_id)
    uploaded = await media.upload_reference_image(
        media_type=body.mediaType, file_name=body.filename, data=body.data
    )
    return MediaUploadResponse(
        url=uploaded.url,
        mediaType=uploaded.media_type,
        size=uploaded.size,
        expiresAt=uploaded.expires_at,
    )


@router.get(
    "/jobs",
    response_model=MediaJobsListResponse,
    summary="Список задач генерации",
    description=(
        "Лента генераций пользователя, новые сверху. Только чтение: у незавершённых задач "
        "отдаётся последнее известное состояние без обращения к провайдеру — обновляйте нужную "
        "задачу через `GET /v1/media/jobs/{jobId}`. `kind` фильтрует по типу генерации. "
        "Пролистывание — курсором: передайте `nextCursor` из предыдущего ответа в `cursor`; "
        "`nextCursor: null` означает, что страниц больше нет. Фильтр `kind` при пролистывании "
        "должен оставаться тем же."
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
    cursor: Annotated[
        str | None,
        Query(description="Курсор следующей страницы — `nextCursor` из предыдущего ответа."),
    ] = None,
) -> MediaJobsListResponse:
    await _rate_limit(current.user_id)
    feed = await media.list_jobs(
        user_id=current.user_id, limit=limit, kind=kind, cursor=_decode_cursor(cursor)
    )
    return MediaJobsListResponse(
        jobs=[_job_response(view) for view in feed.items], nextCursor=feed.next_cursor
    )


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


@router.delete(
    "/jobs/{job_id}",
    response_model=MediaJobDeleteResponse,
    summary="Удалить задачу генерации",
    description=(
        "Убирает завершённую задачу из ленты. Удаляется только запись у нас — сам файл остаётся "
        "у провайдера до истечения его срока хранения, мы им не владеем. Задачу в статусе "
        "`queued`/`running` удалить нельзя (`409 job_not_terminal`): возврат кредитов при провале "
        "у провайдера привязан к этой записи и срабатывает при опросе, поэтому сначала доведите "
        "задачу опросом до `completed`/`failed`. Чужая или уже удалённая задача — `404`. "
        "Удаление исходной задачи не удаляет сделанные из неё правки — у них просто обнуляется "
        "`parentJobId`."
    ),
)
async def delete_media_job(
    request: Request,
    current: CurrentUser,
    media: Annotated[MediaGenerationService, Depends(get_media_generation_service)],
    job_id: Annotated[uuid.UUID, Path(description="Идентификатор задачи генерации.")],
) -> MediaJobDeleteResponse:
    await _rate_limit(current.user_id)
    await media.delete_job(user_id=current.user_id, job_id=job_id)
    return MediaJobDeleteResponse(deleted=True)
