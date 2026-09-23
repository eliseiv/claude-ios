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

ADR-108 §6: proxy jobs (``provider <> ''``) are always in the selection — their tick makes NO
outgoing call (a replay of the completion path from ``pending_result`` or the deadline). The
selection is ``provider <> '' ∨ fal_configured ∨ created_at < now − MEDIA_JOB_DEADLINE_SECONDS``,
oldest first. ADR-108 §10: every tick also refreshes ``media_proxy_jobs_awaiting_callback``.
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
from app.observability.metrics import media_proxy_jobs_awaiting_callback

logger = logging.getLogger("app.media_generation.reconciler")

# ADR-108 §10: a proxy job without a callback for longer than this is counted by the gauge. A code
# constant, not a setting: the longest `completed` measured across the fleet is 2310 s; one hour
# is above it and six times below the 6 h deadline.
MEDIA_PROXY_CALLBACK_OVERDUE_SECONDS = 3600


async def reconcile_once(settings: Settings | None = None) -> int:
    """Advance up to ``MEDIA_RECONCILE_BATCH_SIZE`` non-terminal jobs. Returns count advanced.

    With a fal key: every non-terminal job, oldest first. Without one (ADR-105 §B7): only jobs
    older than ``MEDIA_JOB_DEADLINE_SECONDS`` — the service closes each of them by the deadline
    without an outgoing call (the fal client refuses before any request when the key is empty) —
    plus every proxy job regardless of age (ADR-108 §6).
    """
    settings = settings or get_settings()
    configured = settings.fal_configured()
    now = datetime.datetime.now(tz=datetime.UTC)
    created_before: datetime.datetime | None = None
    if not configured:
        created_before = now - datetime.timedelta(seconds=settings.media_job_deadline_seconds)
    batch = max(1, settings.media_reconcile_batch_size)
    maker = get_sessionmaker()
    advanced = 0
    async with maker() as session:
        try:
            repo = MediaJobsRepository(session)
            media_proxy_jobs_awaiting_callback.set(
                await repo.count_awaiting_callback(
                    created_before=now
                    - datetime.timedelta(seconds=MEDIA_PROXY_CALLBACK_OVERDUE_SECONDS)
                )
            )
            jobs = await repo.list_non_terminal(
                limit=batch, created_before=created_before, include_proxy_jobs=True
            )
            if not jobs:
                await session.commit()
                return 0
            service = deps.build_media_generation_service(
                session, deps.get_request_log_writer(session)
            )
            # A proxy job is advanced under a row lock (ADR-108 §4.2) that lives until this
            # batch commits. Legacy jobs poll fal over HTTP, so they go first: a lock taken
            # before them would keep a concurrent callback of that job waiting for every poll.
            # The selection itself stays oldest-first.
            jobs.sort(key=lambda row: bool(row.provider))
            for job in jobs:
                try:
                    # ADR-108 §6: SKIP LOCKED for proxy rows — never wait for a concurrent
                    # callback/poll while this batch already holds row and wallet locks.
                    await service.advance(job, skip_locked=True)
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
