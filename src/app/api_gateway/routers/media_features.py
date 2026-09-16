"""Backend-only API for avatar speech, saved avatars, backgrounds and virtual makeup."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response

from app import instance_config
from app.api_gateway.rate_limit import enforce_other_limits
from app.api_gateway.routers.media import media_job_response
from app.api_gateway.routers.presets import resolve_presets_locale
from app.config import get_settings
from app.deps import CurrentUser, get_media_features_service, get_request_log_writer
from app.errors import AppError, RateLimitedError
from app.media_generation.features_service import (
    FeaturePresetView,
    MediaFeaturesService,
    UserAvatarView,
)
from app.request_logs.service import RequestLogWriter
from app.schemas.media import MediaJobDeleteResponse, MediaJobResponse
from app.schemas.media_features import (
    AvatarSpeechOptionsResponse,
    AvatarSpeechRequest,
    BackgroundPresetsResponse,
    MakeupCreateRequest,
    MakeupPresetsResponse,
    MediaFeaturePresetSchema,
    SpeechLanguage,
    SpeechMood,
    SpeechOptionSchema,
    SpeechVoiceSchema,
    SystemAvatarsResponse,
    UserAvatarCreateRequest,
    UserAvatarPrepareRequest,
    UserAvatarSchema,
    UserAvatarsResponse,
)

router = APIRouter(prefix="/v1/media", tags=["Media Features"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


def _preset_schema(item: FeaturePresetView) -> MediaFeaturePresetSchema:
    return MediaFeaturePresetSchema(
        id=item.id,
        feature=cast(Literal["avatar", "background", "makeup"], item.feature),
        title=item.title,
        gender=cast(Literal["male", "female"] | None, item.gender),
        style=item.style,
        imageUrl=item.image_url,
        sortOrder=item.sort_order,
    )


def _user_avatar_schema(item: UserAvatarView) -> UserAvatarSchema:
    row = item.row
    return UserAvatarSchema(
        id=row.id,
        title=row.title,
        sourceSystemAvatarId=row.source_preset_id,
        imageUrl=item.image_url,
        status="preparing" if row.preparation_job_id is not None else "ready",
        preparationJobId=row.preparation_job_id,
        createdAt=row.created_at,
        updatedAt=row.updated_at,
    )


@router.get("/avatar-speech/options", response_model=AvatarSpeechOptionsResponse)
async def avatar_speech_options(
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> AvatarSpeechOptionsResponse:
    """Language affects pronunciation only; submitted text is never translated."""
    await _rate_limit(current.user_id)
    resolved = resolve_presets_locale(
        query_locale=locale,
        accept_language=accept_language,
        default_locale=instance_config.presets_default_locale(),
    )
    raw = features.speech_options(resolved)
    return AvatarSpeechOptionsResponse(
        enabled=bool(raw["enabled"]),
        configured=bool(raw["configured"]),
        credits=int(cast(int, raw["credits"])),
        maxTextLength=int(cast(int, raw["maxTextLength"])),
        languages=[
            SpeechOptionSchema.model_validate(item) for item in cast(list[object], raw["languages"])
        ],
        moods=[
            SpeechOptionSchema.model_validate(item) for item in cast(list[object], raw["moods"])
        ],
        voices=[
            SpeechVoiceSchema.model_validate(item) for item in cast(list[object], raw["voices"])
        ],
    )


@router.get("/avatars", response_model=SystemAvatarsResponse)
async def list_system_avatars(
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    gender: Literal["male", "female"] | None = Query(default=None),
    style: str | None = Query(default=None, min_length=1, max_length=64),
) -> SystemAvatarsResponse:
    await _rate_limit(current.user_id)
    items = await features.list_system_avatars(gender=gender, style=style)
    enabled = get_settings().avatar_speech_enabled
    return SystemAvatarsResponse(
        enabled=enabled,
        genders=["female", "male"] if enabled else [],
        styles=sorted({item.style for item in items if item.style}),
        avatars=[_preset_schema(item) for item in items],
    )


@router.get("/avatar-backgrounds", response_model=BackgroundPresetsResponse)
async def list_avatar_backgrounds(
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
) -> BackgroundPresetsResponse:
    await _rate_limit(current.user_id)
    items = await features.list_background_presets()
    return BackgroundPresetsResponse(
        enabled=get_settings().avatar_speech_enabled,
        presets=[_preset_schema(item) for item in items],
    )


@router.get("/makeup/presets", response_model=MakeupPresetsResponse)
async def list_makeup_presets(
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
) -> MakeupPresetsResponse:
    await _rate_limit(current.user_id)
    items = await features.list_makeup_presets()
    settings = get_settings()
    return MakeupPresetsResponse(
        enabled=settings.makeup_enabled,
        credits=settings.makeup_credit_cost,
        presets=[_preset_schema(item) for item in items],
    )


@router.get("/features/presets/{presetId}/image")
async def get_media_feature_preset_image(
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    preset_id: Annotated[str, Path(alias="presetId")],
) -> Response:
    media_type, content = await features.get_preset_image(preset_id)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/user-avatars", response_model=UserAvatarSchema, status_code=201)
async def create_user_avatar(
    body: UserAvatarCreateRequest,
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
) -> UserAvatarSchema:
    await _rate_limit(current.user_id)
    item = await features.create_user_avatar(
        user_id=current.user_id,
        title=body.title,
        image_media_type=body.image.mediaType if body.image else None,
        image_data=body.image.data if body.image else None,
        system_avatar_id=body.systemAvatarId,
    )
    return _user_avatar_schema(item)


@router.get("/user-avatars", response_model=UserAvatarsResponse)
async def list_user_avatars(
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
) -> UserAvatarsResponse:
    await _rate_limit(current.user_id)
    items = await features.list_user_avatars(current.user_id)
    return UserAvatarsResponse(avatars=[_user_avatar_schema(item) for item in items])


@router.get("/user-avatars/{avatarId}/image")
async def get_user_avatar_image(
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    avatar_id: Annotated[uuid.UUID, Path(alias="avatarId")],
) -> Response:
    await _rate_limit(current.user_id)
    media_type, content = await features.get_user_avatar_image(
        user_id=current.user_id, avatar_id=avatar_id
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/user-avatars/{avatarId}/prepare", response_model=UserAvatarSchema, status_code=202)
async def prepare_user_avatar(
    body: UserAvatarPrepareRequest,
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    avatar_id: Annotated[uuid.UUID, Path(alias="avatarId")],
) -> UserAvatarSchema:
    await _rate_limit(current.user_id)
    background = body.background
    item = await features.prepare_user_avatar(
        user_id=current.user_id,
        avatar_id=avatar_id,
        remove_background=body.removeBackground,
        background_type=background.type,
        background_color=background.color,
        background_preset_id=background.presetId,
        background_media_type=background.image.mediaType if background.image else None,
        background_data=background.image.data if background.image else None,
    )
    return _user_avatar_schema(item)


@router.delete("/user-avatars/{avatarId}", response_model=MediaJobDeleteResponse)
async def delete_user_avatar(
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    avatar_id: Annotated[uuid.UUID, Path(alias="avatarId")],
) -> MediaJobDeleteResponse:
    await _rate_limit(current.user_id)
    await features.delete_user_avatar(user_id=current.user_id, avatar_id=avatar_id)
    return MediaJobDeleteResponse(deleted=True)


@router.post("/avatar-speech", response_model=MediaJobResponse, status_code=202)
async def create_avatar_speech(
    body: AvatarSpeechRequest,
    request: Request,
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
) -> MediaJobResponse:
    await _rate_limit(current.user_id)
    log_id = await request_logs.start(
        user_id=current.user_id,
        endpoint=request.url.path,
        prompt=body.text,
    )
    try:
        view = await features.create_avatar_speech(
            user_id=current.user_id,
            system_avatar_id=body.systemAvatarId,
            user_avatar_id=body.userAvatarId,
            text=body.text,
            language=cast(SpeechLanguage, body.language),
            voice_id=body.voiceId,
            mood=cast(SpeechMood, body.mood),
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
    return media_job_response(view)


@router.post("/makeup", response_model=MediaJobResponse, status_code=202)
async def create_makeup(
    body: MakeupCreateRequest,
    request: Request,
    current: CurrentUser,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
) -> MediaJobResponse:
    await _rate_limit(current.user_id)
    log_id = await request_logs.start(
        user_id=current.user_id,
        endpoint=request.url.path,
        prompt=body.presetId,
    )
    try:
        view = await features.create_makeup(
            user_id=current.user_id,
            preset_id=body.presetId,
            image_media_type=body.image.mediaType,
            image_data=body.image.data,
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
    return media_job_response(view)
