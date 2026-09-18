"""In-process poller for due scheduled chat tasks (ADR-107, sample: media reconciler)."""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.orchestrator import ChatRunOut
from app.chat.repository import ChatRepository
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.deps import get_v2_orchestrator
from app.errors import AppError
from app.models import ScheduledChatTask
from app.notifications.apns_client import ApnsClient
from app.notifications.push_service import ScheduledChatPushService
from app.observability.logging import log_event
from app.scheduled_chats.repository import ScheduledChatsRepository
from app.schemas.chat import GENERATION_MODE_ORDER, GenerationMode

logger = logging.getLogger("app.scheduled_chats.worker")


def _map_run_outcome(out: ChatRunOut) -> tuple[str, str | None, str | None]:
    """Return ``(status, error_code, error_message)`` from ``ChatRunOut`` (architecture table)."""
    if out.status == "assistant_message":
        return "completed", None, None
    if out.status == "tool_call":
        return "failed", "tool_loop_unsupported", "client-side tool loop is not supported"
    if out.status == "blocked":
        reason = out.block_reason or "blocked"
        if reason == "max_tokens":
            return "failed", "max_tokens", "response truncated at max tokens"
        return "failed", reason, reason
    return "failed", "upstream_error", f"unexpected chat status: {out.status}"


def _generation_mode(raw: str | None) -> GenerationMode:
    if raw in GENERATION_MODE_ORDER:
        return cast(GenerationMode, raw)
    return "general"


async def _push_snapshot(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    status: str,
    session_id: uuid.UUID | None,
    message_step_id: uuid.UUID | None,
    error_code: str | None,
) -> None:
    push = ScheduledChatPushService(session, apns=ApnsClient(get_settings()))
    await push.notify_scheduled_chat_ready(
        task_id=task_id,
        user_id=user_id,
        status=status if status in ("completed", "failed") else "failed",
        session_id=session_id,
        message_step_id=message_step_id,
        error_code=error_code,
    )


def _snapshot(
    task: ScheduledChatTask,
) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID | None, uuid.UUID | None, str | None]:
    return (
        task.id,
        task.user_id,
        task.status,
        task.result_session_id or task.session_id,
        task.result_message_step_id,
        task.error_code,
    )


async def _execute_task(task_id: uuid.UUID, settings: Settings) -> None:
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            repo = ScheduledChatsRepository(session)
            chats = ChatRepository(session)
            result = await session.get(ScheduledChatTask, task_id)
            if result is None or result.status != "running":
                await session.commit()
                return
            task = result
            now = datetime.datetime.now(tz=datetime.UTC)
            push_args: (
                tuple[uuid.UUID, uuid.UUID, str, uuid.UUID | None, uuid.UUID | None, str | None]
                | None
            ) = None

            if task.session_id is not None:
                sess = await chats.get_session(task.session_id, task.user_id)
                if sess is None:
                    marked = await repo.mark_terminal(
                        task_id=task.id,
                        status="failed",
                        now=now,
                        error_code="session_not_found",
                        error_message="planned session no longer exists",
                    )
                    if marked is not None:
                        push_args = _snapshot(marked)
                    await session.commit()
                    if push_args is not None:
                        tid, uid, st, sid, mid, ecode = push_args
                        async with maker() as push_session:
                            await _push_snapshot(
                                push_session,
                                task_id=tid,
                                user_id=uid,
                                status=st,
                                session_id=sid,
                                message_step_id=mid,
                                error_code=ecode,
                            )
                            await push_session.commit()
                    return

            generation_mode = _generation_mode(task.generation_mode)
            orchestrator = get_v2_orchestrator(session)
            planned_session_id = task.session_id
            try:
                out = await orchestrator.run(
                    user_id=task.user_id,
                    project_id=None,
                    session_id=planned_session_id,
                    message=task.prompt,
                    mode=task.mode,
                    assistant_mode=task.assistant_mode if planned_session_id is None else None,
                    model=task.model if planned_session_id is None else None,
                    generation_mode=generation_mode,
                    generation_backend="v2",
                    # ADR-107 §1 / BR-SC-6: planned UUID must resume that row only — soft-TTL
                    # create-on-miss and missing-row create are forbidden.
                    resume_only=planned_session_id is not None,
                )
                status, error_code, error_message = _map_run_outcome(out)
                result_session_id: uuid.UUID | None = out.session_id
                result_message_step_id: uuid.UUID | None = out.message_step_id
                # Defense in depth: any silent resume→new (TOCTOU delete / soft-TTL) → fail.
                if planned_session_id is not None and out.session_id != planned_session_id:
                    status = "failed"
                    error_code = "session_not_found"
                    error_message = "planned session was replaced during run"
                    result_session_id = None
                    result_message_step_id = None
                marked = await repo.mark_terminal(
                    task_id=task.id,
                    status=status,
                    now=datetime.datetime.now(tz=datetime.UTC),
                    result_session_id=result_session_id,
                    result_message_step_id=result_message_step_id,
                    error_code=error_code,
                    error_message=error_message,
                )
            except AppError as exc:
                marked = await repo.mark_terminal(
                    task_id=task.id,
                    status="failed",
                    now=datetime.datetime.now(tz=datetime.UTC),
                    error_code=exc.code,
                    error_message=exc.message,
                )
            except Exception:  # noqa: BLE001 - one bad task must not stall the poller
                log_event(
                    logger,
                    logging.WARNING,
                    "scheduled_chat_execute_error",
                    scheduledChatId=str(task.id),
                )
                marked = await repo.mark_terminal(
                    task_id=task.id,
                    status="failed",
                    now=datetime.datetime.now(tz=datetime.UTC),
                    error_code="upstream_error",
                    error_message="unexpected error during scheduled chat run",
                )
            if marked is not None:
                push_args = _snapshot(marked)
            await session.commit()
            if push_args is not None:
                tid, uid, st, sid, mid, ecode = push_args
                async with maker() as push_session:
                    await _push_snapshot(
                        push_session,
                        task_id=tid,
                        user_id=uid,
                        status=st,
                        session_id=sid,
                        message_step_id=mid,
                        error_code=ecode,
                    )
                    await push_session.commit()
        except Exception:
            await session.rollback()
            log_event(
                logger,
                logging.WARNING,
                "scheduled_chat_task_batch_error",
                scheduledChatId=str(task_id),
            )
            raise


async def poll_once(settings: Settings | None = None) -> int:
    """Recover stuck running + claim due batch + execute. Returns number of claimed tasks."""
    settings = settings or get_settings()
    batch = max(1, settings.scheduled_chat_batch_size)
    ttl = max(1, settings.scheduled_chat_running_ttl_seconds)
    maker = get_sessionmaker()
    claimed_ids: list[uuid.UUID] = []

    async with maker() as session:
        try:
            repo = ScheduledChatsRepository(session)
            now = datetime.datetime.now(tz=datetime.UTC)
            cutoff = now - datetime.timedelta(seconds=ttl)
            recovered = await repo.recover_stuck_running(cutoff=cutoff, now=now)
            snapshots = [_snapshot(row) for row in recovered]
            await session.commit()
            for tid, uid, st, sid, mid, ecode in snapshots:
                async with maker() as push_session:
                    await _push_snapshot(
                        push_session,
                        task_id=tid,
                        user_id=uid,
                        status=st,
                        session_id=sid,
                        message_step_id=mid,
                        error_code=ecode,
                    )
                    await push_session.commit()
        except Exception:
            await session.rollback()
            log_event(logger, logging.WARNING, "scheduled_chat_recovery_error")
            raise

    async with maker() as session:
        try:
            repo = ScheduledChatsRepository(session)
            now = datetime.datetime.now(tz=datetime.UTC)
            claimed = await repo.claim_due(now=now, batch_size=batch)
            claimed_ids = [row.id for row in claimed]
            await session.commit()
        except Exception:
            await session.rollback()
            log_event(logger, logging.WARNING, "scheduled_chat_claim_error")
            raise

    for claimed_id in claimed_ids:
        try:
            await _execute_task(claimed_id, settings)
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                logging.WARNING,
                "scheduled_chat_task_error",
                scheduledChatId=str(claimed_id),
            )
    return len(claimed_ids)


async def worker_loop(stop: asyncio.Event, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    interval = settings.scheduled_chat_poll_seconds
    if interval <= 0:
        return
    log_event(
        logger,
        logging.INFO,
        "scheduled_chat_worker_started",
        intervalSeconds=interval,
        batchSize=settings.scheduled_chat_batch_size,
        runningTtlSeconds=settings.scheduled_chat_running_ttl_seconds,
    )
    while not stop.is_set():
        try:
            await poll_once(settings)
        except Exception:  # noqa: BLE001
            log_event(logger, logging.WARNING, "scheduled_chat_worker_loop_error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
    log_event(logger, logging.INFO, "scheduled_chat_worker_stopped")
