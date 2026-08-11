"""Media gallery templates: /v1/media/templates/* (ADR-066).

JWT list for images/videos; public cover GET. Independent of FAL_API_KEY — the catalog is
readable even when generation is disabled on the instance.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_media_templates_service
from app.errors import RateLimitedError
from app.media_generation.catalog import KIND_IMAGE, KIND_VIDEO
from app.media_generation.templates_service import MediaTemplatesService, TemplateListItem
from app.schemas.media_templates import MediaTemplateItemSchema, MediaTemplatesResponse

router = APIRouter(prefix="/v1/media/templates", tags=["Media Templates"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


def _list_response(items: list[TemplateListItem]) -> MediaTemplatesResponse:
    return MediaTemplatesResponse(
        templates=[
            MediaTemplateItemSchema(
                id=item.id,
                title=item.title,
                coverUrl=item.cover_url,
                prompt=item.prompt,
                model=item.model,
                requiredInputImages=item.required_input_images,
                parameters=item.parameters,
            )
            for item in items
        ]
    )


@router.get(
    "/images",
    response_model=MediaTemplatesResponse,
    summary="Каталог шаблонов генерации фото",
    description=(
        "Плитки image-шаблонов для галереи iOS: обложка, промпт, модель, параметры и сколько "
        "фото попросить у юзера. Не зависит от `FAL_API_KEY`."
    ),
)
async def list_image_templates(
    current: CurrentUser,
    templates: Annotated[MediaTemplatesService, Depends(get_media_templates_service)],
) -> MediaTemplatesResponse:
    await _rate_limit(current.user_id)
    return _list_response(await templates.list_kind(KIND_IMAGE))


@router.get(
    "/videos",
    response_model=MediaTemplatesResponse,
    summary="Каталог шаблонов генерации видео",
    description=(
        "Плитки video-шаблонов для галереи iOS. Параллельно с `/templates/images`. Не зависит "
        "от `FAL_API_KEY`."
    ),
)
async def list_video_templates(
    current: CurrentUser,
    templates: Annotated[MediaTemplatesService, Depends(get_media_templates_service)],
) -> MediaTemplatesResponse:
    await _rate_limit(current.user_id)
    return _list_response(await templates.list_kind(KIND_VIDEO))


@router.get(
    "/{templateId}/cover",
    summary="Обложка шаблона",
    description=(
        "Байты обложки плитки. Без JWT — для `AsyncImage`/кэша. `Cache-Control: public, "
        "max-age=86400`."
    ),
    responses={
        200: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}}},
        404: {"description": "Шаблон не найден."},
    },
)
async def get_template_cover(
    template_id: Annotated[str, Path(alias="templateId")],
    templates: Annotated[MediaTemplatesService, Depends(get_media_templates_service)],
) -> Response:
    cover = await templates.get_cover(template_id)
    return Response(
        content=cover.data,
        media_type=cover.media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
