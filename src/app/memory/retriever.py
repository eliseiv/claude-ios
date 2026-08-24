"""Hybrid retrieval (vector + keyword) and prompt formatting."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from app.config import Settings
from app.memory.embedding import EmbeddingClient
from app.memory.repository import ChunkSearchHit, MemoryRepository

_MEMORY_INTENT_RE = re.compile(
    r"(?i)(что мы (?:обсуждали|говорили|писали)|найди (?:в )?(?:прошлых|старых)|"
    r"напомни (?:про|о|об)|вспомни|из прошлого|what did we (?:discuss|talk)|"
    r"find (?:in )?(?:past|previous|old) (?:chat|conversation)|remember when|"
    r"recall (?:our|the) (?:chat|conversation))",
)

_RETRIEVAL_INSTRUCTION = (
    "Relevant excerpts from the user's past conversations are provided below. "
    "Use them when answering questions about prior discussions across chats and projects. "
    "When citing, mention the chat title if available. "
    "If nothing is relevant, say so — do not invent past conversations."
)

_EXPLICIT_MEMORY_INSTRUCTION = (
    "Known facts about the user (saved memory) are listed below. "
    "Use them consistently across chats when relevant."
)


@dataclass(frozen=True)
class SearchResultView:
    session_id: uuid.UUID
    message_step_id: uuid.UUID
    workspace_project_id: uuid.UUID | None
    title: str | None
    snippet: str
    role: str
    score: float
    created_at: str


def should_auto_retrieve(message: str) -> bool:
    return bool(_MEMORY_INTENT_RE.search(message.strip()))


def resolve_memory_search(
    *,
    memory_search: bool | None,
    message: str,
    memory_enabled: bool,
) -> bool:
    if not memory_enabled:
        return False
    if memory_search is True:
        return True
    if memory_search is False:
        return False
    return should_auto_retrieve(message)


def reciprocal_rank_fusion(
    *ranked_lists: list[ChunkSearchHit],
    k: int = 60,
) -> list[ChunkSearchHit]:
    scores: dict[uuid.UUID, float] = {}
    by_id: dict[uuid.UUID, ChunkSearchHit] = {}
    for results in ranked_lists:
        for rank, hit in enumerate(results):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank + 1)
            by_id[hit.chunk_id] = hit
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    merged: list[ChunkSearchHit] = []
    for chunk_id, rrf_score in ordered:
        hit = by_id[chunk_id]
        merged.append(
            ChunkSearchHit(
                chunk_id=hit.chunk_id,
                session_id=hit.session_id,
                message_step_id=hit.message_step_id,
                workspace_project_id=hit.workspace_project_id,
                session_title=hit.session_title,
                role=hit.role,
                text=hit.text,
                created_at=hit.created_at,
                score=rrf_score,
            )
        )
    return merged


class MemoryRetriever:
    def __init__(
        self,
        repo: MemoryRepository,
        embedder: EmbeddingClient,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._embedder = embedder
        self._settings = settings

    async def hybrid_search(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        limit: int | None = None,
        workspace_project_id: uuid.UUID | None = None,
        scope: str = "global",
        exclude_session_id: uuid.UUID | None = None,
    ) -> list[ChunkSearchHit]:
        top_k = limit or self._settings.memory_search_top_k
        vector_limit = max(top_k * 2, 10)
        query_vec = (await self._embedder.embed([query]))[0]
        vector_hits = await self._repo.vector_search(
            user_id=user_id,
            query_embedding=query_vec,
            limit=vector_limit,
            workspace_project_id=workspace_project_id,
            scope=scope,
            exclude_session_id=exclude_session_id,
        )
        keyword_hits = await self._repo.keyword_search(
            user_id=user_id,
            query=query,
            limit=vector_limit,
            workspace_project_id=workspace_project_id,
            scope=scope,
            exclude_session_id=exclude_session_id,
        )
        merged = reciprocal_rank_fusion(vector_hits, keyword_hits)
        return merged[:top_k]

    def format_retrieval_block(
        self,
        hits: list[ChunkSearchHit],
        *,
        max_chars: int,
    ) -> str | None:
        if not hits:
            return None
        lines: list[str] = [_RETRIEVAL_INSTRUCTION, ""]
        used = len(lines[0]) + 1
        for hit in hits:
            title = hit.session_title or "Untitled chat"
            date = hit.created_at.date().isoformat()
            header = f'[Chat "{title}", {date}, role={hit.role}]'
            body = hit.text.strip()
            snippet = body[: min(len(body), max(80, max_chars - used - len(header) - 4))]
            block = f"{header}\n{snippet}"
            if used + len(block) + 2 > max_chars:
                break
            lines.append(block)
            lines.append("")
            used += len(block) + 2
        if len(lines) <= 2:
            return None
        return "\n".join(lines).strip()

    def format_explicit_memories(
        self,
        memories: list[str],
        *,
        max_chars: int,
    ) -> str | None:
        if not memories:
            return None
        lines = [_EXPLICIT_MEMORY_INSTRUCTION, ""]
        used = len(lines[0]) + 1
        for fact in memories:
            line = f"- {fact.strip()}"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        if len(lines) <= 2:
            return None
        return "\n".join(lines).strip()

    @staticmethod
    def to_search_views(
        hits: list[ChunkSearchHit], *, snippet_limit: int = 240
    ) -> list[SearchResultView]:
        views: list[SearchResultView] = []
        for hit in hits:
            snippet = hit.text.strip()
            if len(snippet) > snippet_limit:
                snippet = snippet[: snippet_limit - 1] + "…"
            views.append(
                SearchResultView(
                    session_id=hit.session_id,
                    message_step_id=hit.message_step_id,
                    workspace_project_id=hit.workspace_project_id,
                    title=hit.session_title,
                    snippet=snippet,
                    role=hit.role,
                    score=round(hit.score, 4),
                    created_at=hit.created_at.isoformat(),
                )
            )
        return views
