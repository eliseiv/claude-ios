"""Schemas for /v1/scheduled-chats (modules/scheduled-chats/02-api-contracts.md, ADR-107)."""

from __future__ import annotations

import datetime
import uuid
from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel

ScheduledChatStatus = Literal["scheduled", "running", "completed", "failed", "cancelled"]
BillingMode = Literal["credits", "byok"]
AssistantMode = Literal["chat", "code"]
GenerationMode = Literal["general", "research", "reasoning", "study_learn"]


class ScheduledChatCreateRequest(StrictModel):
    prompt: str = Field(description="Текст хода; после strip непустой.")
    runAt: datetime.datetime = Field(description="ISO8601 tz-aware момент запуска.")
    sessionId: uuid.UUID | None = Field(
        default=None,
        description="Resume существующей owned-сессии; null/omit — новая сессия при запуске.",
    )
    mode: str | None = Field(
        default=None,
        description="billing_mode; default credits. Игнорируется, если sessionId задан.",
    )
    assistantMode: str | None = Field(default=None, description="chat | code (для новой сессии).")
    model: str | None = Field(default=None, description="Модель allowlist инстанса (новая сессия).")
    generationMode: str | None = Field(
        default=None,
        description="general | research | reasoning | study_learn; null → general на запуске.",
    )


class ScheduledChatPatchRequest(StrictModel):
    prompt: str | None = None
    runAt: datetime.datetime | None = None
    sessionId: uuid.UUID | None = None
    mode: str | None = None
    assistantMode: str | None = None
    model: str | None = None
    generationMode: str | None = None


class ScheduledChatResponse(StrictModel):
    id: uuid.UUID
    sessionId: uuid.UUID | None
    prompt: str
    mode: BillingMode
    assistantMode: AssistantMode | None
    model: str | None
    generationMode: GenerationMode | None
    runAt: datetime.datetime
    status: ScheduledChatStatus
    resultSessionId: uuid.UUID | None
    resultMessageStepId: uuid.UUID | None
    errorCode: str | None
    errorMessage: str | None
    claimedAt: datetime.datetime | None
    startedAt: datetime.datetime | None
    finishedAt: datetime.datetime | None
    pushSentAt: datetime.datetime | None
    createdAt: datetime.datetime
    updatedAt: datetime.datetime


class ScheduledChatListResponse(StrictModel):
    items: list[ScheduledChatResponse]
    nextCursor: str | None = None


class ScheduledChatDeleteResponse(StrictModel):
    deleted: bool = True
    status: Literal["cancelled"] | None = None
