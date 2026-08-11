"""Send push on media job completion (ADR-067).

Idempotent: ``media_jobs.push_sent_at`` is claimed before any APNs call so poll and the
background reconciler cannot double-notify. Failures are logged and swallowed — a push
outage must never undo a completed generation or a wallet refund.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaJob
from app.notifications.apns_client import ApnsClient, MediaReadyPush, media_ready_copy
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
