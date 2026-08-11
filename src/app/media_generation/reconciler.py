"""Background reconciler for non-terminal media jobs (ADR-067, closes Q-060-2).

Client polling alone cannot advance a job after iOS freezes the app (~30s background).
This loop periodically polls fal for stuck ``queued``/``running`` rows using the same
``MediaGenerationService._advance`` path as ``GET /v1/media/jobs/{id}``, so completion
still refunds failures and can emit the media-ready push.
"""

from __future__ import annotations

import asyncio
import logging

from app.audit.service import AuditService
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.media_generation.fal_client import FalClient
from app.media_generation.repository import MediaJobsRepository
from app.media_generation.service import MediaGenerationService
from app.notifications.apns_client import ApnsClient
from app.notifications.push_service import MediaPushService
from app.observability.logging import log_event
from app.wallet.service import WalletService

logger = logging.getLogger("app.media_generation.reconciler")


async def reconcile_once(settings: Settings | None = None) -> int:
    """Advance up to ``MEDIA_RECONCILE_BATCH_SIZE`` non-terminal jobs. Returns count advanced."""
    settings = settings or get_settings()
    if not settings.fal_api_key.strip():
        return 0
    batch = max(1, settings.media_reconcile_batch_size)
    maker = get_sessionmaker()
    advanced = 0
    async with maker() as session:
        try:
            repo = MediaJobsRepository(session)
            jobs = await repo.list_non_terminal(limit=batch)
            if not jobs:
                await session.commit()
                return 0
            fal = FalClient(settings)
            wallet = WalletService(session, AuditService(session))
            push = MediaPushService(session, apns=ApnsClient(settings))
            service = MediaGenerationService(
                repo=repo, fal=fal, wallet=wallet, settings=settings, push=push
            )
            for job in jobs:
                try:
                    await service.advance(job)
                    advanced += 1
                except Exception:  # noqa: BLE001 - one bad job must not stall the batch
                    log_event(
                        logger,
                        logging.WARNING,
                        "media_reconcile_job_error",
                        jobId=str(job.id),
                    )
            await session.commit()
        except Exception:
            await session.rollback()
            log_event(logger, logging.WARNING, "media_reconcile_batch_error")
            raise
    return advanced


async def reconciler_loop(stop: asyncio.Event, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    interval = settings.media_reconcile_interval_seconds
    if interval <= 0:
        return
    log_event(
        logger,
        logging.INFO,
        "media_reconciler_started",
        intervalSeconds=interval,
        batchSize=settings.media_reconcile_batch_size,
    )
    while not stop.is_set():
        try:
            await reconcile_once(settings)
        except Exception:  # noqa: BLE001
            log_event(logger, logging.WARNING, "media_reconcile_loop_error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
    log_event(logger, logging.INFO, "media_reconciler_stopped")
