"""Index chat steps into pgvector chunks (async background + backfill)."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.memory.embedding import EmbeddingClient, get_embedding_client
from app.memory.repository import MemoryRepository
from app.memory.text import chunk_text, extract_step_text
from app.models import ChatSession, ChatStep

logger = logging.getLogger("app.memory.indexer")


class MemoryIndexer:
    def __init__(
        self,
        session: AsyncSession,
        embedder: EmbeddingClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._repo = MemoryRepository(session)
        self._embedder = embedder
        self._settings = settings

    async def index_step(self, chat_step_id: uuid.UUID) -> bool:
        if not self._settings.memory_enabled:
            return False
        if not self._embedder.configured:
            return False
        row = await self._session.execute(
            select(ChatStep, ChatSession)
            .join(ChatSession, ChatSession.id == ChatStep.session_id)
            .where(ChatStep.id == chat_step_id)
        )
        pair = row.one_or_none()
        if pair is None:
            return False
        step, sess = pair
        if sess.is_temporary:
            return False
        if step.role not in ("user", "assistant"):
            return False
        plain = extract_step_text(step.payload, role=step.role)
        if not plain:
            return False
        parts = chunk_text(
            plain,
            max_chars=self._settings.memory_chunk_max_chars,
            overlap=self._settings.memory_chunk_overlap_chars,
        )
        vectors = await self._embedder.embed(parts)
        chunks = [
            (idx, part, vec) for idx, (part, vec) in enumerate(zip(parts, vectors, strict=True))
        ]
        await self._repo.upsert_chunks(
            user_id=sess.user_id,
            session_id=sess.id,
            chat_step_id=step.id,
            message_step_id=step.message_step_id,
            workspace_project_id=sess.workspace_project_id,
            session_title=sess.title,
            role=step.role,
            chunks=chunks,
        )
        await self._session.commit()
        return True

    async def index_turn(self, session_id: uuid.UUID, message_step_id: uuid.UUID) -> None:
        rows = await self._session.scalars(
            select(ChatStep.id).where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role.in_(("user", "assistant")),
            )
        )
        for step_id in rows:
            await self.index_step(step_id)

    async def delete_from_message_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> None:
        anchor_seq = await self._session.scalar(
            select(ChatStep.seq)
            .where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role == "user",
            )
            .order_by(ChatStep.seq.asc())
            .limit(1)
        )
        if anchor_seq is None:
            return
        await self._repo.delete_chunks_from_seq(session_id, int(anchor_seq))
        await self._session.commit()

    async def backfill_user(
        self,
        user_id: uuid.UUID,
        *,
        batch_size: int = 100,
        max_batches: int = 100,
    ) -> int:
        indexed = 0
        after: uuid.UUID | None = None
        for _ in range(max_batches):
            batch = await self._repo.list_steps_for_backfill(
                user_id=user_id, after_step_id=after, limit=batch_size
            )
            if not batch:
                break
            for step, _sess in batch:
                if await self.index_step(step.id):
                    indexed += 1
                after = step.id
            if len(batch) < batch_size:
                break
        return indexed


def schedule_index_turn(session_id: uuid.UUID, message_step_id: uuid.UUID) -> None:
    settings = get_settings()
    if not settings.memory_enabled:
        return

    async def _run() -> None:
        try:
            maker = get_sessionmaker()
            async with maker() as session:
                indexer = MemoryIndexer(session, get_embedding_client(), settings)
                await indexer.index_turn(session_id, message_step_id)
        except Exception:
            logger.exception(
                "memory_index_turn_failed",
                extra={"sessionId": str(session_id), "messageStepId": str(message_step_id)},
            )

    asyncio.create_task(_run())


def schedule_delete_from_message_step(session_id: uuid.UUID, message_step_id: uuid.UUID) -> None:
    settings = get_settings()
    if not settings.memory_enabled:
        return

    async def _run() -> None:
        try:
            maker = get_sessionmaker()
            async with maker() as session:
                indexer = MemoryIndexer(session, get_embedding_client(), settings)
                await indexer.delete_from_message_step(session_id, message_step_id)
        except Exception:
            logger.exception(
                "memory_delete_turn_failed",
                extra={"sessionId": str(session_id), "messageStepId": str(message_step_id)},
            )

    asyncio.create_task(_run())


def schedule_delete_session_chunks(session_id: uuid.UUID) -> None:
    settings = get_settings()
    if not settings.memory_enabled:
        return

    async def _run() -> None:
        try:
            maker = get_sessionmaker()
            async with maker() as session:
                repo = MemoryRepository(session)
                await repo.delete_chunks_for_session(session_id)
                await session.commit()
        except Exception:
            logger.exception("memory_delete_session_failed", extra={"sessionId": str(session_id)})

    asyncio.create_task(_run())
