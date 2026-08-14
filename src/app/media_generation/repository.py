"""Persistence for ``media_jobs`` (media-generation/03-architecture.md).

Every query is scoped ``WHERE user_id = :sub``, so a foreign job is indistinguishable from a
missing one (the service turns both into 404). Nothing here commits: the request-scoped session
from ``session_scope()`` commits once at the end, which is what makes "debit credits + create job"
a single transaction — if the fal submit between them fails, the debit rolls back with it.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.media_generation.cursor import MediaJobCursor
from app.models import MediaJob

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})
NON_TERMINAL_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})


@dataclass(frozen=True)
class MediaJobsPage:
    """One page of the feed plus the cursor that resumes after it (``None`` = last page)."""

    items: list[MediaJob]
    next_cursor: str | None


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
        provider_cost_usd: float | None = None,
        parent_job_id: uuid.UUID | None = None,
        input_image_urls: list[str] | None = None,
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
            provider_cost_usd=(
                None if provider_cost_usd is None else decimal.Decimal(str(provider_cost_usd))
            ),
            credits_refunded=False,
            parent_job_id=parent_job_id,
            input_image_urls=input_image_urls or None,
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
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        kind: str | None = None,
        cursor: MediaJobCursor | None = None,
    ) -> MediaJobsPage:
        """One page of the owner's feed, newest first, keyset-paginated on (created_at, id).

        Fetches ``limit + 1`` rows so the next cursor is known without a second count query: if the
        extra row came back there is more feed, and the cursor points at the last row we return.
        """
        stmt = select(MediaJob).where(MediaJob.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(MediaJob.kind == kind)
        if cursor is not None:
            stmt = stmt.where(
                (MediaJob.created_at < cursor.created_at)
                | ((MediaJob.created_at == cursor.created_at) & (MediaJob.id < cursor.id))
            )
        stmt = stmt.order_by(MediaJob.created_at.desc(), MediaJob.id.desc()).limit(limit + 1)
        rows = list((await self._session.scalars(stmt)).all())
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            MediaJobCursor(created_at=items[-1].created_at, id=items[-1].id).encode()
            if has_more and items
            else None
        )
        return MediaJobsPage(items=items, next_cursor=next_cursor)

    async def delete(self, job: MediaJob) -> None:
        """Delete a job the caller has already fetched (and therefore already owner-checked).

        Takes the row rather than an id so ownership is proven by the ``get`` that produced it —
        there is no second place where the scoping could be forgotten. No commit: the
        request-scoped session commits once, as everywhere in this repository.
        """
        await self._session.delete(job)
        await self._session.flush()

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

    async def list_non_terminal(self, *, limit: int) -> list[MediaJob]:
        """Oldest non-terminal jobs first — for the background reconciler (ADR-067).

        Not owner-scoped: the reconciler is a trusted in-process worker that advances every
        stuck job so refunds and media-ready pushes still happen when the client stops polling.
        """
        stmt = (
            select(MediaJob)
            .where(MediaJob.status.in_(tuple(NON_TERMINAL_STATUSES)))
            .order_by(MediaJob.created_at.asc(), MediaJob.id.asc())
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())
