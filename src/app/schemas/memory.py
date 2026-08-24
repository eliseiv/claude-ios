"""Schemas for /v1/search and /v1/memories."""

from __future__ import annotations

import uuid

from pydantic import Field

from app.schemas.common import StrictModel


class SearchResultSchema(StrictModel):
    sessionId: uuid.UUID
    messageStepId: uuid.UUID
    workspaceProjectId: uuid.UUID | None = None
    title: str | None = None
    snippet: str
    role: str
    score: float
    createdAt: str


class SearchResponse(StrictModel):
    results: list[SearchResultSchema]


class MemoryItemSchema(StrictModel):
    id: uuid.UUID
    content: str
    workspaceProjectId: uuid.UUID | None = None
    source: str
    createdAt: str
    updatedAt: str


class MemoryListResponse(StrictModel):
    items: list[MemoryItemSchema]


class MemoryCreateRequest(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    workspaceProjectId: uuid.UUID | None = Field(
        default=None,
        description="Null = global memory; UUID = scoped to workspace project.",
    )


class MemoryCreateResponse(StrictModel):
    memory: MemoryItemSchema


class MemoryDeleteResponse(StrictModel):
    deleted: bool = True


class MemoryBackfillRequest(StrictModel):
    userId: uuid.UUID
    batchSize: int = Field(default=100, ge=1, le=500)


class MemoryBackfillResponse(StrictModel):
    indexedSteps: int
    stats: dict[str, int]


class MemoryStatsResponse(StrictModel):
    chunks: int
    memories: int
    unindexedSteps: int
