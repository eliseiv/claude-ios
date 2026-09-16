"""Admin CRUD for the empty-by-default avatar/background/makeup catalog."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Path, Request

from app.api_gateway.rate_limit import enforce_admin_limits
from app.config import get_settings
from app.deps import client_ip, get_media_features_service
from app.errors import PayloadTooLargeError, RateLimitedError
from app.media_generation.features_service import MediaFeaturesService
from app.schemas.media_features import (
    MediaFeaturePresetCreateRequest,
    MediaFeaturePresetDeleteResponse,
    MediaFeaturePresetSchema,
)

router = APIRouter(prefix="/media/features/presets", tags=["Admin Media Features"])


async def _rate_limit(request: Request) -> None:
    if not await enforce_admin_limits(ip=client_ip(request)):
        raise RateLimitedError("admin rate limit exceeded")


@router.post("", response_model=MediaFeaturePresetSchema, status_code=201)
async def create_media_feature_preset(
    request: Request,
    body: MediaFeaturePresetCreateRequest,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
) -> MediaFeaturePresetSchema:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > get_settings().media_upload_request_body_limit:
                raise PayloadTooLargeError("admin media feature body exceeds limit")
        except ValueError:
            pass
    await _rate_limit(request)
    item = await features.create_preset(
        preset_id=body.id,
        feature=body.feature,
        title=body.title,
        gender=body.gender,
        style=body.style,
        provider_value=body.providerValue,
        image_media_type=body.image.mediaType,
        image_data=body.image.data,
        sort_order=body.sortOrder,
    )
    return MediaFeaturePresetSchema(
        id=item.id,
        feature=cast(Literal["avatar", "background", "makeup"], item.feature),
        title=item.title,
        gender=cast(Literal["male", "female"] | None, item.gender),
        style=item.style,
        imageUrl=item.image_url,
        sortOrder=item.sort_order,
    )


@router.delete("/{presetId}", response_model=MediaFeaturePresetDeleteResponse)
async def delete_media_feature_preset(
    request: Request,
    features: Annotated[MediaFeaturesService, Depends(get_media_features_service)],
    preset_id: Annotated[str, Path(alias="presetId")],
) -> MediaFeaturePresetDeleteResponse:
    await _rate_limit(request)
    await features.delete_preset(preset_id)
    return MediaFeaturePresetDeleteResponse(deleted=True)
