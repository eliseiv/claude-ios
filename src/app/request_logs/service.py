"""Independent, best-effort persistence for CRM request history (ADR-077)."""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RequestLog

logger = logging.getLogger(__name__)

_PREVIEW_LIMIT = 200


def _preview(value: str | None) -> str | None:
    if not value:
        return None
    return value if len(value) <= _PREVIEW_LIMIT else value[:_PREVIEW_LIMIT] + "…"


class RequestLogWriter:
    """Write request lifecycle in transactions independent of business work.

    Telemetry must survive a rollback of the request-scoped session, while a
    telemetry outage must never change the API response.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def start(
        self, *, user_id: uuid.UUID, endpoint: str, prompt: str | None = None
    ) -> uuid.UUID | None:
        log_id = uuid.uuid4()
        try:
            async with self._sessionmaker.begin() as session:
                session.add(
                    RequestLog(
                        id=log_id,
                        user_id=user_id,
                        endpoint=endpoint,
                        prompt_preview=_preview(prompt),
                        status="started",
                        status_code=202,
                        refunded=False,
                    )
                )
            return log_id
        except SQLAlchemyError:
            logger.exception("request_log_start_failed", extra={"requestLogId": str(log_id)})
            return None

    async def finish_chat(
        self,
        log_id: uuid.UUID | None,
        *,
        status_code: int,
        message_step_id: uuid.UUID | None,
        tokens_spent: int,
    ) -> None:
        if log_id is None:
            return
        try:
            async with self._sessionmaker.begin() as session:
                row = await session.get(RequestLog, log_id, with_for_update=True)
                if row is None or row.completed_at is not None:
                    return
                row.message_step_id = message_step_id
                row.status = "completed"
                row.status_code = status_code
                row.tokens_spent = Decimal(tokens_spent)
                row.completed_at = datetime.datetime.now(tz=datetime.UTC)
        except SQLAlchemyError:
            logger.exception("request_log_finish_failed", extra={"requestLogId": str(log_id)})

    async def fail(self, log_id: uuid.UUID | None, *, status_code: int) -> None:
        if log_id is None:
            return
        try:
            async with self._sessionmaker.begin() as session:
                row = await session.get(RequestLog, log_id, with_for_update=True)
                if row is None or row.completed_at is not None:
                    return
                row.status = "failed"
                row.status_code = status_code
                row.tokens_spent = Decimal(0)
                row.completed_at = datetime.datetime.now(tz=datetime.UTC)
        except SQLAlchemyError:
            logger.exception("request_log_fail_failed", extra={"requestLogId": str(log_id)})

    async def queue_media(
        self,
        log_id: uuid.UUID | None,
        *,
        media_job_id: uuid.UUID,
        tokens_spent: int,
    ) -> None:
        if log_id is None:
            return
        try:
            async with self._sessionmaker.begin() as session:
                row = await session.get(RequestLog, log_id, with_for_update=True)
                if row is None or row.completed_at is not None:
                    return
                row.status = "queued"
                row.status_code = 202
                row.media_job_id = media_job_id
                row.tokens_spent = Decimal(tokens_spent)
        except SQLAlchemyError:
            logger.exception("request_log_queue_failed", extra={"requestLogId": str(log_id)})

    async def finish_media(
        self,
        *,
        media_job_id: uuid.UUID,
        failed: bool,
        refunded: bool,
    ) -> None:
        try:
            async with self._sessionmaker.begin() as session:
                row = await session.scalar(
                    select(RequestLog)
                    .where(RequestLog.media_job_id == media_job_id)
                    .with_for_update()
                )
                if row is None or row.completed_at is not None:
                    return
                row.status = "failed" if failed else "completed"
                row.status_code = 500 if failed else 200
                row.refunded = refunded
                row.completed_at = datetime.datetime.now(tz=datetime.UTC)
        except SQLAlchemyError:
            logger.exception(
                "request_log_media_finish_failed", extra={"mediaJobId": str(media_job_id)}
            )
