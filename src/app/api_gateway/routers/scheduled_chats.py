"""Scheduled chats routes: /v1/scheduled-chats (ADR-107)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_scheduled_chats_service
from app.errors import RateLimitedError
from app.scheduled_chats.service import ScheduledChatsService
from app.schemas.scheduled_chats import (
    ScheduledChatCreateRequest,
    ScheduledChatDeleteResponse,
    ScheduledChatListResponse,
    ScheduledChatPatchRequest,
    ScheduledChatResponse,
)

router = APIRouter(prefix="/v1/scheduled-chats", tags=["ScheduledChats"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


@router.post(
    "",
    response_model=ScheduledChatResponse,
    status_code=201,
    summary="Создать запланированную чат-задачу",
    description=(
        "Одноразовая задача: prompt + runAt (+ опциональный sessionId). "
        "Кредиты при создании не списываются — биллинг в момент запуска воркером."
    ),
)
async def create_scheduled_chat(
    body: ScheduledChatCreateRequest,
    request: Request,
    current: CurrentUser,
    service: Annotated[ScheduledChatsService, Depends(get_scheduled_chats_service)],
) -> ScheduledChatResponse:
    await _rate_limit(current.user_id)
    return await service.create(user_id=current.user_id, body=body)


@router.get(
    "",
    response_model=ScheduledChatListResponse,
    summary="Список запланированных чат-задач",
)
async def list_scheduled_chats(
    request: Request,
    current: CurrentUser,
    service: Annotated[ScheduledChatsService, Depends(get_scheduled_chats_service)],
    status: Annotated[str | None, Query(description="Фильтр статуса")] = None,
    cursor: Annotated[str | None, Query(description="Opaque cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ScheduledChatListResponse:
    await _rate_limit(current.user_id)
    return await service.list(user_id=current.user_id, status=status, cursor=cursor, limit=limit)


@router.get(
    "/{id}",
    response_model=ScheduledChatResponse,
    summary="Получить запланированную чат-задачу",
)
async def get_scheduled_chat(
    request: Request,
    current: CurrentUser,
    service: Annotated[ScheduledChatsService, Depends(get_scheduled_chats_service)],
    id: Annotated[uuid.UUID, Path(description="Id задачи")],
) -> ScheduledChatResponse:
    await _rate_limit(current.user_id)
    return await service.get(user_id=current.user_id, task_id=id)


@router.patch(
    "/{id}",
    response_model=ScheduledChatResponse,
    summary="Обновить запланированную чат-задачу",
    description="Только пока status=scheduled; иначе 409 not_patchable.",
)
async def patch_scheduled_chat(
    body: ScheduledChatPatchRequest,
    request: Request,
    current: CurrentUser,
    service: Annotated[ScheduledChatsService, Depends(get_scheduled_chats_service)],
    id: Annotated[uuid.UUID, Path(description="Id задачи")],
) -> ScheduledChatResponse:
    await _rate_limit(current.user_id)
    return await service.patch(user_id=current.user_id, task_id=id, body=body)


@router.delete(
    "/{id}",
    response_model=ScheduledChatDeleteResponse,
    summary="Отменить или удалить запланированную чат-задачу",
)
async def delete_scheduled_chat(
    request: Request,
    current: CurrentUser,
    service: Annotated[ScheduledChatsService, Depends(get_scheduled_chats_service)],
    id: Annotated[uuid.UUID, Path(description="Id задачи")],
) -> ScheduledChatDeleteResponse:
    await _rate_limit(current.user_id)
    return await service.delete(user_id=current.user_id, task_id=id)
