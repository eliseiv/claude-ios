"""Memory routes: cross-chat search, explicit facts, admin backfill."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api_gateway.auth import require_admin
from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_memory_service
from app.errors import RateLimitedError, ValidationFailedError
from app.memory.repository import MemoryRow
from app.memory.service import MemoryService
from app.schemas.memory import (
    MemoryBackfillRequest,
    MemoryBackfillResponse,
    MemoryCreateRequest,
    MemoryCreateResponse,
    MemoryDeleteResponse,
    MemoryItemSchema,
    MemoryListResponse,
    MemoryStatsResponse,
    SearchResponse,
    SearchResultSchema,
)

router = APIRouter(tags=["Memory"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


def _memory_item(row: MemoryRow) -> MemoryItemSchema:
    return MemoryItemSchema(
        id=row.id,
        content=row.content,
        workspaceProjectId=row.workspace_project_id,
        source=row.source,
        createdAt=row.created_at.isoformat(),
        updatedAt=row.updated_at.isoformat(),
    )


@router.get(
    "/v1/search",
    response_model=SearchResponse,
    summary="Семантический поиск по всем чатам",
    description=(
        "Hybrid RAG-поиск (vector + keyword) по индексированным сообщениям пользователя. "
        "Требует включённой памяти (`memoryEnabled` в preferences). Не списывает кредиты."
    ),
)
async def search_chats(
    request: Request,
    current: CurrentUser,
    memory: Annotated[MemoryService, Depends(get_memory_service)],
    q: Annotated[str, Query(min_length=1, max_length=500, description="Поисковый запрос")],
    limit: Annotated[int | None, Query(ge=1, le=50)] = None,
    workspaceProjectId: Annotated[
        uuid.UUID | None, Query(description="Фильтр по workspace (optional)")
    ] = None,
    scope: Annotated[str | None, Query(description="global | workspace")] = None,
) -> SearchResponse:
    await _rate_limit(current.user_id)
    if scope is not None and scope not in ("global", "workspace"):
        raise ValidationFailedError("scope must be global or workspace")
    hits = await memory.search(
        current.user_id,
        query=q,
        limit=limit,
        workspace_project_id=workspaceProjectId,
        scope=scope,
    )
    return SearchResponse(
        results=[
            SearchResultSchema(
                sessionId=h.session_id,
                messageStepId=h.message_step_id,
                workspaceProjectId=h.workspace_project_id,
                title=h.title,
                snippet=h.snippet,
                role=h.role,
                score=h.score,
                createdAt=h.created_at,
            )
            for h in hits
        ]
    )


@router.get(
    "/v1/memories",
    response_model=MemoryListResponse,
    summary="Список сохранённых фактов",
)
async def list_memories(
    request: Request,
    current: CurrentUser,
    memory: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryListResponse:
    await _rate_limit(current.user_id)
    rows = await memory.list_memories(current.user_id)
    return MemoryListResponse(items=[_memory_item(row) for row in rows])


@router.post(
    "/v1/memories",
    response_model=MemoryCreateResponse,
    status_code=201,
    summary="Сохранить факт в память",
)
async def create_memory(
    body: MemoryCreateRequest,
    request: Request,
    current: CurrentUser,
    memory: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryCreateResponse:
    await _rate_limit(current.user_id)
    row = await memory.create_memory(
        current.user_id,
        content=body.content,
        workspace_project_id=body.workspaceProjectId,
    )
    return MemoryCreateResponse(memory=_memory_item(row))


@router.delete(
    "/v1/memories/{memoryId}",
    response_model=MemoryDeleteResponse,
    summary="Удалить сохранённый факт",
)
async def delete_memory(
    memoryId: Annotated[uuid.UUID, Path(description="ID факта")],
    request: Request,
    current: CurrentUser,
    memory: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryDeleteResponse:
    await _rate_limit(current.user_id)
    await memory.delete_memory(current.user_id, memoryId)
    return MemoryDeleteResponse()


@router.get(
    "/v1/memories/stats",
    response_model=MemoryStatsResponse,
    summary="Статистика индекса памяти",
)
async def memory_stats(
    request: Request,
    current: CurrentUser,
    memory: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryStatsResponse:
    await _rate_limit(current.user_id)
    stats = await memory.stats(current.user_id)
    return MemoryStatsResponse(**stats)


admin_router = APIRouter(
    prefix="/v1/admin/memory",
    tags=["Admin"],
    dependencies=[Depends(require_admin)],
)


@admin_router.post(
    "/backfill",
    response_model=MemoryBackfillResponse,
    summary="Backfill индекса памяти для пользователя",
)
async def admin_backfill(
    body: MemoryBackfillRequest,
    memory: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryBackfillResponse:
    indexed = await memory.backfill(body.userId, batch_size=body.batchSize)
    stats = await memory.stats(body.userId)
    return MemoryBackfillResponse(indexedSteps=indexed, stats=stats)
