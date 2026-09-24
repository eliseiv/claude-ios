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

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from app.config import get_settings
from app.media_generation.cursor import MediaJobCursor
from app.models import MediaJob

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})

# `media_jobs.provider_cost_usd` is NUMERIC(12,6): at most 6 integer digits.
_PROVIDER_COST_LIMIT = decimal.Decimal(10) ** 6
# ADR-108 §8: proxy services whose `vendor_price` is CONFIRMED to be USD (pilot 2026-09-24). Only
# for them the callback price replaces the cost CRM reads; `kie` joins when Q-108-12 confirms it.
USD_CONFIRMED_PROXY_SERVICES = frozenset({"fal", "sosana"})
NON_TERMINAL_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})

# ADR-109 §7: states of OUR copy of a result (`media_jobs.asset_store_status`).
ASSET_STORE_NONE = ""
ASSET_STORE_PENDING = "pending"
ASSET_STORE_STORED = "stored"
ASSET_STORE_FAILED = "failed"
ASSET_STORE_MISSING = "missing"
ASSET_STORE_EXPIRED = "expired"


def _has_assets(result: dict[str, Any] | None) -> bool:
    """Whether a normalized result carries at least one asset with a URL (ADR-109 §1)."""
    if not isinstance(result, dict):
        return False
    return any(
        isinstance(item, dict) and isinstance(item.get("url"), str) and item.get("url")
        for item in result.get("assets") or []
    )


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
        moderation: dict[str, Any] | None = None,
        operation: str = "generation",
        operation_input: dict[str, Any] | None = None,
        visible_in_history: bool = True,
        provider: str = "",
    ) -> MediaJob:
        # `provider` is passed explicitly (never left to the server default): an attribute the
        # INSERT did not set is expired after flush, and an async lazy load of it would fail.
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
            moderation=moderation,
            operation=operation,
            operation_input=operation_input,
            visible_in_history=visible_in_history,
            provider=provider,
            vendor_price=None,
            pending_result=None,
            # ADR-109 §7 — set explicitly for the same reason as `provider` above.
            asset_store_status=ASSET_STORE_NONE,
            asset_store_attempts=0,
            asset_store_next_attempt_at=None,
            assets_expire_at=None,
            assets_stored_at=None,
            assets_stored_bytes=None,
            stored_assets=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, *, job_id: uuid.UUID, user_id: uuid.UUID) -> MediaJob | None:
        row: MediaJob | None = await self._session.scalar(
            select(MediaJob).where(MediaJob.id == job_id, MediaJob.user_id == user_id)
        )
        return row

    async def get_by_id(self, job_id: uuid.UUID) -> MediaJob | None:
        """Primary-key lookup for the signed download route (ADR-085).

        Owner isolation is the HMAC (jobId + ownerUserId + index), not this query. Same
        exception class as ``list_non_terminal``: a trusted in-process caller, not a user id.
        """
        row: MediaJob | None = await self._session.scalar(
            select(MediaJob).where(MediaJob.id == job_id)
        )
        return row

    async def get_for_update(
        self, job_id: uuid.UUID, *, skip_locked: bool = False
    ) -> MediaJob | None:
        """``SELECT … FOR UPDATE`` by id, refreshing the identity-map copy (ADR-108 §4.2).

        Taken by the webhook AND by the proxy branch of ``_advance``: without the row lock a
        ``completed`` callback and a deadline close of the same job could each write their own
        terminal, and the later ORM flush would overwrite the earlier one. Not owner-scoped: the
        webhook caller is authorized by the HMAC of the job id, the poll caller already proved
        ownership with ``get``.

        ``skip_locked`` (the reconciler only): a row another session holds is NOT waited for —
        ``None`` comes back and the row stays for the next tick. The reconciler keeps its locks
        until the batch commits and writes wallets on refunds, so waiting there could close a
        lock cycle with a concurrent webhook/poll of the same user.
        """
        row: MediaJob | None = await self._session.scalar(
            select(MediaJob)
            .where(MediaJob.id == job_id)
            .with_for_update(skip_locked=skip_locked)
            .execution_options(populate_existing=True)
        )
        return row

    def savepoint(self) -> AsyncSessionTransaction:
        """A nested transaction (``SAVEPOINT``) on the request session (ADR-108 §5, webhook)."""
        return self._session.begin_nested()

    async def refresh(self, job: MediaJob) -> None:
        """Reload a row after a rolled-back savepoint expired its attributes."""
        await self._session.refresh(job)

    async def store_pending_result(self, job: MediaJob, *, pending_result: dict[str, Any]) -> None:
        """Record the callback result before the shared completion path runs (ADR-108 §4.3)."""
        job.pending_result = pending_result
        job.updated_at = _now()
        await self._session.flush()

    async def record_vendor_price(self, job: MediaJob, *, vendor_price: decimal.Decimal) -> None:
        """Record the vendor's actual price; for USD-confirmed services make it the CRM cost.

        ADR-108 §8: ``vendor_price`` always keeps the callback value as is. ``provider_cost_usd``
        — the only cost column CRM reads — is REPLACED only when ``job.provider`` is in
        ``USD_CONFIRMED_PROXY_SERVICES``; for ``kie`` and any other service the submit-time
        estimate stays, otherwise CRM would silently get a cost in foreign units. A value the
        ``NUMERIC(12,6)`` of ``provider_cost_usd`` cannot hold leaves the estimate in place too: a
        write that overflows would fail the whole callback transaction.
        """
        job.vendor_price = vendor_price
        if job.provider in USD_CONFIRMED_PROXY_SERVICES and vendor_price < _PROVIDER_COST_LIMIT:
            job.provider_cost_usd = vendor_price
        job.updated_at = _now()
        await self._session.flush()

    async def count_awaiting_callback(self, *, created_before: datetime.datetime) -> int:
        """Proxy jobs with no callback applied, older than ``created_before`` (ADR-108 §10).

        ``provider <> '' ∧ status ∈ {queued, running} ∧ pending_result IS NULL ∧
        created_at < created_before`` — one aggregate query per reconciler tick.
        """
        value = await self._session.scalar(
            select(func.count())
            .select_from(MediaJob)
            .where(
                MediaJob.provider != "",
                MediaJob.status.in_(tuple(NON_TERMINAL_STATUSES)),
                MediaJob.pending_result.is_(None),
                MediaJob.created_at < created_before,
            )
        )
        return int(value or 0)

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
        stmt = select(MediaJob).where(
            MediaJob.user_id == user_id,
            MediaJob.visible_in_history.is_(True),
        )
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

    async def mark_completed(
        self, job: MediaJob, *, result: dict[str, Any], moderation: dict[str, Any] | None = None
    ) -> None:
        job.status = STATUS_COMPLETED
        job.result = result
        job.error = None
        # ADR-108 §5: the pending result is applied — it does not outlive the terminal.
        job.pending_result = None
        if moderation is not None:
            job.moderation = moderation
        now = _now()
        # ADR-109 §2: the ONLY change of the completion path — with storage on and a non-empty
        # result the row is queued for the background store loop and its retention is fixed
        # once, from this moment. No download, no network call, no new failure here.
        settings = get_settings()
        if settings.media_asset_storage_enabled() and _has_assets(result):
            job.asset_store_status = ASSET_STORE_PENDING
            job.assets_expire_at = now + datetime.timedelta(
                days=settings.media_asset_retention_days()
            )
        job.updated_at = now
        await self._session.flush()

    async def mark_failed(
        self,
        job: MediaJob,
        *,
        error: str,
        refunded: bool,
        moderation: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        job.status = STATUS_FAILED
        job.error = error
        job.credits_refunded = refunded
        job.pending_result = None
        if moderation is not None:
            job.moderation = moderation
        if result is not None:
            # ADR-086 §5: у заблокированного результата ассеты НЕ сохраняются — иначе файл остался
            # бы достижим через signed-URL download-роут (ADR-085), и блокировка стала бы
            # декоративной.
            job.result = result
        job.updated_at = _now()
        await self._session.flush()

    async def list_non_terminal(
        self,
        *,
        limit: int,
        created_before: datetime.datetime | None = None,
        include_proxy_jobs: bool = False,
    ) -> list[MediaJob]:
        """Oldest non-terminal jobs first — for the background reconciler (ADR-067).

        Not owner-scoped: the reconciler is a trusted in-process worker that advances every
        stuck job so refunds and media-ready pushes still happen when the client stops polling.
        ``created_before`` narrows the same selection by age (ADR-105 §B7: with an empty fal key
        only jobs past the deadline are taken) — served by the partial index on ``created_at``.
        ``include_proxy_jobs`` keeps proxy jobs (``provider <> ''``) in that narrowed selection
        regardless of age (ADR-108 §6: their tick makes no outgoing call).
        """
        stmt = select(MediaJob).where(MediaJob.status.in_(tuple(NON_TERMINAL_STATUSES)))
        if created_before is not None:
            age_filter = MediaJob.created_at < created_before
            if include_proxy_jobs:
                stmt = stmt.where(or_(MediaJob.provider != "", age_filter))
            else:
                stmt = stmt.where(age_filter)
        stmt = stmt.order_by(MediaJob.created_at.asc(), MediaJob.id.asc()).limit(limit)
        return list((await self._session.scalars(stmt)).all())
