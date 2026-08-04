"""Persistence for ``media_jobs`` (media-generation/03-architecture.md).

Every query is scoped ``WHERE user_id = :sub``, so a foreign job is indistinguishable from a
missing one (the service turns both into 404). Nothing here commits: the request-scoped session
from ``session_scope()`` commits once at the end, which is what makes "debit credits + create job"
a single transaction — if the fal submit between them fails, the debit rolls back with it.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaJob

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class MediaJobsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        job_id: uuid.UUID,
        user_id: uuid.UUID,
        model_id: str,
        kind: str,
        fal_endpoint: str,
        fal_request_id: str,
        status_url: str,
        response_url: str,
        status: str,
        prompt: str,
        credits_charged: int,
    ) -> MediaJob:
        row = MediaJob(
            id=job_id,
            user_id=user_id,
            model_id=model_id,
            kind=kind,
            fal_endpoint=fal_endpoint,
            fal_request_id=fal_request_id,
            status_url=status_url,
            response_url=response_url,
            status=status,
            prompt=prompt,
            credits_charged=credits_charged,
            credits_refunded=False,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, *, job_id: uuid.UUID, user_id: uuid.UUID) -> MediaJob | None:
        row: MediaJob | None = await self._session.scalar(
            select(MediaJob).where(MediaJob.id == job_id, MediaJob.user_id == user_id)
        )
        return row

    async def list_for_user(
        self, *, user_id: uuid.UUID, limit: int, kind: str | None = None
    ) -> list[MediaJob]:
        stmt = select(MediaJob).where(MediaJob.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(MediaJob.kind == kind)
        stmt = stmt.order_by(MediaJob.created_at.desc(), MediaJob.id.desc()).limit(limit)
        return list((await self._session.scalars(stmt)).all())

    async def mark_running(self, job: MediaJob) -> None:
        if job.status == STATUS_RUNNING:
            return
        job.status = STATUS_RUNNING
        job.updated_at = _now()
        await self._session.flush()

    async def mark_completed(self, job: MediaJob, *, result: dict[str, Any]) -> None:
        job.status = STATUS_COMPLETED
        job.result = result
        job.error = None
        job.updated_at = _now()
        await self._session.flush()

    async def mark_failed(self, job: MediaJob, *, error: str, refunded: bool) -> None:
        job.status = STATUS_FAILED
        job.error = error
        job.credits_refunded = refunded
        job.updated_at = _now()
        await self._session.flush()
