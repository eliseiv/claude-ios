"""Background reconciler for non-terminal media jobs (ADR-067, closes Q-060-2; ADR-105 §B).

Client polling alone cannot advance a job after iOS freezes the app (~30s background).
This loop periodically polls fal for stuck ``queued``/``running`` rows using the same
``MediaGenerationService._advance`` path as ``GET /v1/media/jobs/{id}``, so completion
still refunds failures and can emit the media-ready push.

ADR-105 §B6: the service is built by THE same function as on the request path
(``deps.build_media_generation_service``) — with ``request_logs`` and ``moderation``. A job the
reconciler finishes closes its ``request_logs`` row, and a picture it finds ready passes
post-moderation, exactly as on ``GET``.

ADR-105 §B7/§B8: for a job the client no longer polls, the deadline of §B1 is held ONLY here.
With an empty ``FAL_API_KEY`` there is nothing to poll with, but jobs past the deadline are still
closed as ``failed`` with a refund (``lastObservation = not_configured``) and no request goes
upstream; younger jobs are not touched.
"""

from __future__ import annotations

import asyncio
import datetime
import logging

from app import deps
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.media_generation.repository import MediaJobsRepository
from app.observability.logging import log_event

logger = logging.getLogger("app.media_generation.reconciler")


async def reconcile_once(settings: Settings | None = None) -> int:
    """Advance up to ``MEDIA_RECONCILE_BATCH_SIZE`` non-terminal jobs. Returns count advanced.

    With a fal key: every non-terminal job, oldest first. Without one (ADR-105 §B7): only jobs
    older than ``MEDIA_JOB_DEADLINE_SECONDS`` — the service closes each of them by the deadline
    without an outgoing call (the fal client refuses before any request when the key is empty).
    """
    settings = settings or get_settings()
    configured = bool(settings.fal_api_key.strip())
    created_before: datetime.datetime | None = None
    if not configured:
        created_before = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(
            seconds=settings.media_job_deadline_seconds
        )
    batch = max(1, settings.media_reconcile_batch_size)
    maker = get_sessionmaker()
    advanced = 0
    async with maker() as session:
        try:
            repo = MediaJobsRepository(session)
            jobs = await repo.list_non_terminal(limit=batch, created_before=created_before)
            if not jobs:
                await session.commit()
                return 0
            service = deps.build_media_generation_service(
                session, deps.get_request_log_writer(session)
            )
            for job in jobs:
                try:
                    await service.advance(job)
                    advanced += 1
                except Exception as exc:  # noqa: BLE001 - one bad job must not stall the batch
                    log_event(
                        logger,
                        logging.WARNING,
                        "media_reconcile_job_error",
                        jobId=str(job.id),
                        # ADR-105 §B5: the class name (never the text) — with jobId it attributes
                        # «which job fails with what» without reading `fal_call_outcome` rows.
                        exceptionClass=type(exc).__name__,
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
