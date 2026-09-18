"""Send push on media job completion and scheduled chat terminal states (ADR-067 / ADR-107).

Idempotent: ``push_sent_at`` is claimed before any APNs call so pollers cannot double-notify.
Failures are logged and swallowed — a push outage must never undo a completed generation or a
terminal scheduled-chat status.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaJob, ScheduledChatTask
from app.notifications.apns_client import (
    ApnsClient,
    MediaReadyPush,
    ScheduledChatReadyPush,
    media_ready_copy,
    scheduled_chat_ready_copy,
)
from app.notifications.repository import DevicePushTokensRepository
from app.observability.logging import log_event
from app.preferences.service import PreferencesService

logger = logging.getLogger("app.notifications.push")


class MediaPushService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        apns: ApnsClient,
        tokens: DevicePushTokensRepository | None = None,
        preferences: PreferencesService | None = None,
    ) -> None:
        self._session = session
        self._apns = apns
        self._tokens = tokens or DevicePushTokensRepository(session)
        self._preferences = preferences or PreferencesService(session)

    async def notify_media_ready(
        self,
        *,
        job_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: str,
        media_url: str,
    ) -> None:
        try:
            await self._notify(job_id=job_id, user_id=user_id, kind=kind, media_url=media_url)
        except Exception:  # noqa: BLE001 - push must never break media completion
            log_event(
                logger,
                logging.WARNING,
                "media_push_unexpected_error",
                jobId=str(job_id),
                userId=str(user_id),
            )

    async def _notify(
        self,
        *,
        job_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: str,
        media_url: str,
    ) -> None:
        claimed = await self._claim_push_sent(job_id)
        if not claimed:
            return

        prefs = await self._preferences.get(user_id)
        if not prefs.notifications_enabled:
            log_event(
                logger,
                logging.INFO,
                "media_push_skipped_disabled",
                jobId=str(job_id),
                userId=str(user_id),
            )
            return

        rows = await self._tokens.list_for_user(user_id=user_id)
        if not rows:
            log_event(
                logger,
                logging.INFO,
                "media_push_skipped_no_token",
                jobId=str(job_id),
                userId=str(user_id),
            )
            return

        if not self._apns.configured:
            log_event(
                logger,
                logging.WARNING,
                "media_push_skipped_apns_not_configured",
                jobId=str(job_id),
            )
            return

        title, body = media_ready_copy(kind=kind)
        payload = self._apns.build_media_ready_payload(
            MediaReadyPush(
                job_id=str(job_id),
                kind=kind,
                media_url=media_url,
                title=title,
                body=body,
            )
        )
        sent = 0
        for row in rows:
            result = await self._apns.send(device_token=row.push_token, payload=payload)
            if result == "unregistered":
                await self._tokens.delete_by_push_token(push_token=row.push_token)
            elif result == "sent":
                sent += 1

        log_event(
            logger,
            logging.INFO,
            "media_push_done",
            jobId=str(job_id),
            userId=str(user_id),
            kind=kind,
            devices=len(rows),
            sent=sent,
        )

    async def _claim_push_sent(self, job_id: uuid.UUID) -> bool:
        """Atomically stamp ``push_sent_at``; False if another worker already claimed it."""
        import datetime

        now = datetime.datetime.now(tz=datetime.UTC)
        result = await self._session.execute(
            update(MediaJob)
            .where(
                MediaJob.id == job_id,
                MediaJob.status == "completed",
                MediaJob.push_sent_at.is_(None),
            )
            .values(push_sent_at=now)
            .returning(MediaJob.id)
        )
        return result.scalar_one_or_none() is not None


class ScheduledChatPushService:
    """APNs for scheduled chat terminal states — NOT ``notify_media_ready`` (ADR-107)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        apns: ApnsClient,
        tokens: DevicePushTokensRepository | None = None,
        preferences: PreferencesService | None = None,
    ) -> None:
        self._session = session
        self._apns = apns
        self._tokens = tokens or DevicePushTokensRepository(session)
        self._preferences = preferences or PreferencesService(session)

    async def notify_scheduled_chat_ready(
        self,
        *,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        status: str,
        session_id: uuid.UUID | None,
        message_step_id: uuid.UUID | None,
        error_code: str | None,
    ) -> None:
        try:
            await self._notify(
                task_id=task_id,
                user_id=user_id,
                status=status,
                session_id=session_id,
                message_step_id=message_step_id,
                error_code=error_code,
            )
        except Exception:  # noqa: BLE001 - push must never undo terminal task status
            log_event(
                logger,
                logging.WARNING,
                "scheduled_chat_push_unexpected_error",
                scheduledChatId=str(task_id),
                userId=str(user_id),
            )

    async def _notify(
        self,
        *,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        status: str,
        session_id: uuid.UUID | None,
        message_step_id: uuid.UUID | None,
        error_code: str | None,
    ) -> None:
        claimed = await self._claim_push_sent(task_id)
        if not claimed:
            return

        prefs = await self._preferences.get(user_id)
        if not prefs.notifications_enabled:
            log_event(
                logger,
                logging.INFO,
                "scheduled_chat_push_skipped_disabled",
                scheduledChatId=str(task_id),
                userId=str(user_id),
            )
            return

        rows = await self._tokens.list_for_user(user_id=user_id)
        if not rows:
            log_event(
                logger,
                logging.INFO,
                "scheduled_chat_push_skipped_no_token",
                scheduledChatId=str(task_id),
                userId=str(user_id),
            )
            return

        if not self._apns.configured:
            log_event(
                logger,
                logging.WARNING,
                "scheduled_chat_push_skipped_apns_not_configured",
                scheduledChatId=str(task_id),
            )
            return

        title, body = scheduled_chat_ready_copy(status=status)
        payload = self._apns.build_scheduled_chat_ready_payload(
            ScheduledChatReadyPush(
                scheduled_chat_id=str(task_id),
                session_id=str(session_id) if session_id is not None else None,
                message_step_id=str(message_step_id) if message_step_id is not None else None,
                status=status,
                error_code=error_code,
                title=title,
                body=body,
            )
        )
        sent = 0
        for row in rows:
            result = await self._apns.send(device_token=row.push_token, payload=payload)
            if result == "unregistered":
                await self._tokens.delete_by_push_token(push_token=row.push_token)
            elif result == "sent":
                sent += 1

        log_event(
            logger,
            logging.INFO,
            "scheduled_chat_push_done",
            scheduledChatId=str(task_id),
            userId=str(user_id),
            status=status,
            devices=len(rows),
            sent=sent,
        )

    async def _claim_push_sent(self, task_id: uuid.UUID) -> bool:
        import datetime

        now = datetime.datetime.now(tz=datetime.UTC)
        result = await self._session.execute(
            update(ScheduledChatTask)
            .where(
                ScheduledChatTask.id == task_id,
                ScheduledChatTask.status.in_(("completed", "failed")),
                ScheduledChatTask.push_sent_at.is_(None),
            )
            .values(push_sent_at=now, updated_at=now)
            .returning(ScheduledChatTask.id)
        )
        return result.scalar_one_or_none() is not None
