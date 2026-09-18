"""Persistence for scheduled_chat_tasks (ADR-107 claim = FOR UPDATE SKIP LOCKED)."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ScheduledChatTask
from app.scheduled_chats.cursor import ScheduledChatCursor

ACTIVE_STATUSES = ("scheduled", "running")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class ScheduledChatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, row: ScheduledChatTask) -> ScheduledChatTask:
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_for_user(
        self, *, task_id: uuid.UUID, user_id: uuid.UUID
    ) -> ScheduledChatTask | None:
        result = await self._session.execute(
            select(ScheduledChatTask).where(
                ScheduledChatTask.id == task_id,
                ScheduledChatTask.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def count_active(self, *, user_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(ScheduledChatTask)
            .where(
                ScheduledChatTask.user_id == user_id,
                ScheduledChatTask.status.in_(ACTIVE_STATUSES),
            )
        )
        return int(result.scalar_one())

    async def list_for_user(
        self,
        *,
        user_id: uuid.UUID,
        status: str | None,
        cursor: ScheduledChatCursor | None,
        limit: int,
    ) -> list[ScheduledChatTask]:
        stmt = select(ScheduledChatTask).where(ScheduledChatTask.user_id == user_id)
        if status is not None:
            stmt = stmt.where(ScheduledChatTask.status == status)
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    ScheduledChatTask.created_at < cursor.created_at,
                    and_(
                        ScheduledChatTask.created_at == cursor.created_at,
                        ScheduledChatTask.id < cursor.id,
                    ),
                )
            )
        stmt = stmt.order_by(
            ScheduledChatTask.created_at.desc(), ScheduledChatTask.id.desc()
        ).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def delete(self, row: ScheduledChatTask) -> None:
        await self._session.delete(row)
        await self._session.flush()

    async def claim_due(
        self, *, now: datetime.datetime, batch_size: int
    ) -> list[ScheduledChatTask]:
        """Atomic ``scheduled → running`` with ``FOR UPDATE SKIP LOCKED`` (ADR-107 §2)."""
        ids_stmt = (
            select(ScheduledChatTask.id)
            .where(
                ScheduledChatTask.status == "scheduled",
                ScheduledChatTask.run_at <= now,
            )
            .order_by(ScheduledChatTask.run_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        id_result = await self._session.execute(ids_stmt)
        ids = list(id_result.scalars().all())
        if not ids:
            return []
        await self._session.execute(
            update(ScheduledChatTask)
            .where(
                ScheduledChatTask.id.in_(ids),
                ScheduledChatTask.status == "scheduled",
            )
            .values(
                status="running",
                claimed_at=now,
                started_at=now,
                updated_at=now,
            )
        )
        result = await self._session.execute(
            select(ScheduledChatTask).where(
                ScheduledChatTask.id.in_(ids),
                ScheduledChatTask.status == "running",
            )
        )
        return list(result.scalars().all())

    async def recover_stuck_running(
        self, *, cutoff: datetime.datetime, now: datetime.datetime
    ) -> list[ScheduledChatTask]:
        """Stuck ``running`` past TTL → ``failed`` / ``worker_interrupted``."""
        id_result = await self._session.execute(
            update(ScheduledChatTask)
            .where(
                ScheduledChatTask.status == "running",
                func.coalesce(ScheduledChatTask.started_at, ScheduledChatTask.claimed_at) < cutoff,
            )
            .values(
                status="failed",
                error_code="worker_interrupted",
                error_message="worker interrupted before completion",
                finished_at=now,
                updated_at=now,
            )
            .returning(ScheduledChatTask.id)
        )
        ids = list(id_result.scalars().all())
        if not ids:
            return []
        result = await self._session.execute(
            select(ScheduledChatTask).where(ScheduledChatTask.id.in_(ids))
        )
        return list(result.scalars().all())

    async def mark_terminal(
        self,
        *,
        task_id: uuid.UUID,
        status: str,
        now: datetime.datetime,
        result_session_id: uuid.UUID | None = None,
        result_message_step_id: uuid.UUID | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ScheduledChatTask | None:
        await self._session.execute(
            update(ScheduledChatTask)
            .where(
                ScheduledChatTask.id == task_id,
                ScheduledChatTask.status == "running",
            )
            .values(
                status=status,
                finished_at=now,
                updated_at=now,
                result_session_id=result_session_id,
                result_message_step_id=result_message_step_id,
                error_code=error_code,
                error_message=(error_message[:500] if error_message else None),
            )
        )
        result = await self._session.execute(
            select(ScheduledChatTask).where(ScheduledChatTask.id == task_id)
        )
        row = result.scalar_one_or_none()
        if row is None or row.status != status:
            return None
        return row
