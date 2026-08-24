"""Persistence for chat_chunks and user_memories."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatChunk, ChatSession, ChatStep, UserMemory


@dataclass(frozen=True)
class ChunkSearchHit:
    chunk_id: uuid.UUID
    session_id: uuid.UUID
    message_step_id: uuid.UUID
    workspace_project_id: uuid.UUID | None
    session_title: str | None
    role: str
    text: str
    created_at: datetime.datetime
    score: float


@dataclass(frozen=True)
class MemoryRow:
    id: uuid.UUID
    content: str
    workspace_project_id: uuid.UUID | None
    source: str
    created_at: datetime.datetime
    updated_at: datetime.datetime


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_chunks(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        chat_step_id: uuid.UUID,
        message_step_id: uuid.UUID,
        workspace_project_id: uuid.UUID | None,
        session_title: str | None,
        role: str,
        chunks: list[tuple[int, str, list[float]]],
    ) -> None:
        await self._session.execute(delete(ChatChunk).where(ChatChunk.chat_step_id == chat_step_id))
        for chunk_index, chunk_text, embedding in chunks:
            row = ChatChunk(
                user_id=user_id,
                session_id=session_id,
                chat_step_id=chat_step_id,
                message_step_id=message_step_id,
                workspace_project_id=workspace_project_id,
                session_title=session_title,
                role=role,
                chunk_index=chunk_index,
                text=chunk_text,
                embedding=embedding,
            )
            self._session.add(row)
        await self._session.flush()

    async def delete_chunks_for_session(self, session_id: uuid.UUID) -> None:
        await self._session.execute(delete(ChatChunk).where(ChatChunk.session_id == session_id))

    async def delete_chunks_from_seq(self, session_id: uuid.UUID, min_seq: int) -> None:
        step_ids = select(ChatStep.id).where(
            ChatStep.session_id == session_id,
            ChatStep.seq >= min_seq,
        )
        await self._session.execute(delete(ChatChunk).where(ChatChunk.chat_step_id.in_(step_ids)))

    async def vector_search(
        self,
        *,
        user_id: uuid.UUID,
        query_embedding: list[float],
        limit: int,
        workspace_project_id: uuid.UUID | None = None,
        scope: str = "global",
        exclude_session_id: uuid.UUID | None = None,
    ) -> list[ChunkSearchHit]:
        distance = ChatChunk.embedding.cosine_distance(query_embedding)
        stmt = (
            select(ChatChunk, distance.label("distance"))
            .where(ChatChunk.user_id == user_id)
            .order_by(distance)
            .limit(limit)
        )
        if scope == "workspace" and workspace_project_id is not None:
            stmt = stmt.where(ChatChunk.workspace_project_id == workspace_project_id)
        if exclude_session_id is not None:
            stmt = stmt.where(ChatChunk.session_id != exclude_session_id)
        rows = await self._session.execute(stmt)
        hits: list[ChunkSearchHit] = []
        for chunk, dist in rows.all():
            score = max(0.0, 1.0 - float(dist))
            hits.append(
                ChunkSearchHit(
                    chunk_id=chunk.id,
                    session_id=chunk.session_id,
                    message_step_id=chunk.message_step_id,
                    workspace_project_id=chunk.workspace_project_id,
                    session_title=chunk.session_title,
                    role=chunk.role,
                    text=chunk.text,
                    created_at=chunk.created_at,
                    score=score,
                )
            )
        return hits

    async def keyword_search(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        limit: int,
        workspace_project_id: uuid.UUID | None = None,
        scope: str = "global",
        exclude_session_id: uuid.UUID | None = None,
    ) -> list[ChunkSearchHit]:
        ts_query = func.plainto_tsquery("simple", query)
        rank = func.ts_rank(func.to_tsvector("simple", ChatChunk.text), ts_query)
        stmt = (
            select(ChatChunk, rank.label("rank"))
            .where(
                ChatChunk.user_id == user_id,
                func.to_tsvector("simple", ChatChunk.text).op("@@")(ts_query),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
        if scope == "workspace" and workspace_project_id is not None:
            stmt = stmt.where(ChatChunk.workspace_project_id == workspace_project_id)
        if exclude_session_id is not None:
            stmt = stmt.where(ChatChunk.session_id != exclude_session_id)
        rows = await self._session.execute(stmt)
        hits: list[ChunkSearchHit] = []
        for chunk, rank_val in rows.all():
            hits.append(
                ChunkSearchHit(
                    chunk_id=chunk.id,
                    session_id=chunk.session_id,
                    message_step_id=chunk.message_step_id,
                    workspace_project_id=chunk.workspace_project_id,
                    session_title=chunk.session_title,
                    role=chunk.role,
                    text=chunk.text,
                    created_at=chunk.created_at,
                    score=float(rank_val or 0.0),
                )
            )
        return hits

    async def list_steps_for_backfill(
        self, *, user_id: uuid.UUID, after_step_id: uuid.UUID | None, limit: int
    ) -> list[tuple[ChatStep, ChatSession]]:
        stmt = (
            select(ChatStep, ChatSession)
            .join(ChatSession, ChatSession.id == ChatStep.session_id)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.is_temporary.is_(False),
                ChatStep.role.in_(("user", "assistant")),
            )
            .order_by(ChatStep.seq.asc())
            .limit(limit)
        )
        if after_step_id is not None:
            anchor_seq = await self._session.scalar(
                select(ChatStep.seq).where(ChatStep.id == after_step_id)
            )
            if anchor_seq is not None:
                stmt = stmt.where(ChatStep.seq > anchor_seq)
        rows = await self._session.execute(stmt)
        return list(rows.all())

    async def count_indexed_steps(self, user_id: uuid.UUID) -> int:
        return int(
            await self._session.scalar(
                select(func.count(func.distinct(ChatChunk.chat_step_id))).where(
                    ChatChunk.user_id == user_id
                )
            )
            or 0
        )

    async def list_all_memories(self, user_id: uuid.UUID) -> list[MemoryRow]:
        rows = await self._session.scalars(
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.desc())
        )
        return [
            MemoryRow(
                id=row.id,
                content=row.content,
                workspace_project_id=row.workspace_project_id,
                source=row.source,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    async def list_memories(
        self,
        user_id: uuid.UUID,
        *,
        workspace_project_id: uuid.UUID | None = None,
        scope: str = "global",
    ) -> list[MemoryRow]:
        stmt = (
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.desc())
        )
        if scope == "workspace" and workspace_project_id is not None:
            stmt = stmt.where(UserMemory.workspace_project_id == workspace_project_id)
        elif scope == "global":
            stmt = stmt.where(UserMemory.workspace_project_id.is_(None))
        rows = await self._session.scalars(stmt)
        return [
            MemoryRow(
                id=row.id,
                content=row.content,
                workspace_project_id=row.workspace_project_id,
                source=row.source,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    async def create_memory(
        self,
        *,
        user_id: uuid.UUID,
        content: str,
        workspace_project_id: uuid.UUID | None,
        source: str = "explicit",
    ) -> MemoryRow:
        row = UserMemory(
            user_id=user_id,
            content=content,
            workspace_project_id=workspace_project_id,
            source=source,
        )
        self._session.add(row)
        await self._session.flush()
        return MemoryRow(
            id=row.id,
            content=row.content,
            workspace_project_id=row.workspace_project_id,
            source=row.source,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def delete_memory(self, memory_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(UserMemory).where(
                UserMemory.id == memory_id,
                UserMemory.user_id == user_id,
            )
        )
        return (result.rowcount or 0) > 0

    async def get_memory(self, memory_id: uuid.UUID, user_id: uuid.UUID) -> MemoryRow | None:
        row = await self._session.scalar(
            select(UserMemory).where(
                UserMemory.id == memory_id,
                UserMemory.user_id == user_id,
            )
        )
        if row is None:
            return None
        return MemoryRow(
            id=row.id,
            content=row.content,
            workspace_project_id=row.workspace_project_id,
            source=row.source,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def count_unindexed_steps(self, user_id: uuid.UUID) -> int:
        indexed = select(ChatChunk.chat_step_id)
        stmt = (
            select(func.count())
            .select_from(ChatStep)
            .join(ChatSession, ChatSession.id == ChatStep.session_id)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.is_temporary.is_(False),
                ChatStep.role.in_(("user", "assistant")),
                ChatStep.id.not_in(indexed),
            )
        )
        return int(await self._session.scalar(stmt) or 0)

    async def raw_backfill_stats(self, user_id: uuid.UUID) -> dict[str, Any]:
        chunks = int(
            await self._session.scalar(
                select(func.count()).select_from(ChatChunk).where(ChatChunk.user_id == user_id)
            )
            or 0
        )
        memories = int(
            await self._session.scalar(
                select(func.count()).select_from(UserMemory).where(UserMemory.user_id == user_id)
            )
            or 0
        )
        unindexed = await self.count_unindexed_steps(user_id)
        return {"chunks": chunks, "memories": memories, "unindexedSteps": unindexed}
