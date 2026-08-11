"""Admin CRUD for media gallery templates: /v1/admin/media/templates (ADR-066).

Mounted under the admin router (``require_admin``). Create accepts a base64 cover and therefore
bypasses the 8 KB admin body cap — SizeLimitMiddleware raises the transport limit for this path.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Body, Depends, Path, Request

from app.api_gateway.rate_limit import enforce_admin_limits
from app.config import get_settings
from app.deps import client_ip, get_media_templates_service
from app.errors import PayloadTooLargeError, RateLimitedError
from app.media_generation.templates_service import MediaTemplatesService
from app.schemas.media_templates import (
    MediaTemplateAdminItemSchema,
    MediaTemplateCreateRequest,
    MediaTemplateDeleteResponse,
)

router = APIRouter(prefix="/media/templates", tags=["Admin Media Templates"])


async def _enforce_admin_rate_limit(request: Request) -> None:
    if not await enforce_admin_limits(ip=client_ip(request)):
        raise RateLimitedError("admin rate limit exceeded")


def _enforce_create_body_size(request: Request) -> None:
    """Allow covers up to MEDIA_TEMPLATE_COVER_REQUEST_BODY_LIMIT (not the 8 KB admin cap)."""
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        size = int(content_length)
    except ValueError:
        return
    if size > get_settings().media_template_cover_request_body_limit:
        raise PayloadTooLargeError("admin template cover body exceeds limit")


@router.post(
    "",
    response_model=MediaTemplateAdminItemSchema,
    status_code=201,
    summary="Создать шаблон галереи",
    description=(
        "Добавляет image/video шаблон с base64-обложкой. Конфликт `id` → `409`. Авторизация — "
        "`X-Admin-Token` / `X-Admin-Key`."
    ),
)
async def create_media_template(
    request: Request,
    templates: Annotated[MediaTemplatesService, Depends(get_media_templates_service)],
    body: Annotated[MediaTemplateCreateRequest, Body()],
) -> MediaTemplateAdminItemSchema:
    _enforce_create_body_size(request)
    await _enforce_admin_rate_limit(request)
    item = await templates.create(
        template_id=body.id,
        kind=body.kind,
        title=body.title,
        prompt=body.prompt,
        model_id=body.model,
        required_input_images=body.requiredInputImages,
        parameters=body.parameters,
        cover_media_type=body.cover.mediaType,
        cover_data_b64=body.cover.data,
        sort_order=body.sortOrder,
    )
    return MediaTemplateAdminItemSchema(
        id=item.id,
        kind=cast(Literal["image", "video"], item.kind),
        title=item.title,
        coverUrl=item.cover_url,
        prompt=item.prompt,
        model=item.model,
        requiredInputImages=item.required_input_images,
        parameters=item.parameters,
        sortOrder=item.sort_order,
    )


@router.delete(
    "/{templateId}",
    response_model=MediaTemplateDeleteResponse,
    summary="Удалить шаблон галереи",
    description="Удаляет шаблон и обложку. Несуществующий `id` → `404`.",
)
async def delete_media_template(
    request: Request,
    templates: Annotated[MediaTemplatesService, Depends(get_media_templates_service)],
    template_id: Annotated[str, Path(alias="templateId")],
) -> MediaTemplateDeleteResponse:
    # Delete stays under the normal admin 8 KB cap (no body).
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > get_settings().admin_size_limit_body:
                raise PayloadTooLargeError("admin request body exceeds limit")
        except ValueError:
            pass
    await _enforce_admin_rate_limit(request)
    await templates.delete(template_id)
    return MediaTemplateDeleteResponse(deleted=True)
