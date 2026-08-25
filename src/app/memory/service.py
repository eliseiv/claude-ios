"""Memory use-cases: search, explicit facts, orchestrator context assembly."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.errors import NotFoundError, ValidationFailedError
from app.memory.embedding import EmbeddingClient, get_embedding_client
from app.memory.indexer import MemoryIndexer
from app.memory.repository import MemoryRepository, MemoryRow
from app.memory.retriever import MemoryRetriever, SearchResultView, resolve_memory_search
from app.workspaces.service import WorkspacesService


class MemoryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder: EmbeddingClient | None = None,
        settings: Settings | None = None,
        workspaces: WorkspacesService | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._repo = MemoryRepository(session)
        self._embedder = embedder or get_embedding_client()
        self._retriever = MemoryRetriever(self._repo, self._embedder, self._settings)
        self._indexer = MemoryIndexer(session, self._embedder, self._settings)
        self._workspaces = workspaces

    async def search(
        self,
        user_id: uuid.UUID,
        *,
        query: str,
        limit: int | None = None,
        workspace_project_id: uuid.UUID | None = None,
        scope: str | None = None,
    ) -> list[SearchResultView]:
        if not self._settings.memory_enabled:
            raise ValidationFailedError("memory is disabled on this instance")
        q = query.strip()
        if not q:
            raise ValidationFailedError("query must not be empty")
        effective_scope = scope or "global"
        hits = await self._retriever.hybrid_search(
            user_id=user_id,
            query=q,
            limit=limit,
            workspace_project_id=workspace_project_id,
            scope=effective_scope,
        )
        return self._retriever.to_search_views(hits)

    async def build_context_for_turn(
        self,
        *,
        user_id: uuid.UUID,
        message: str,
        memory_search: bool | None,
        memory_search_scope: str,
        workspace_project_id: uuid.UUID | None,
        exclude_session_id: uuid.UUID | None,
    ) -> str | None:
        if not resolve_memory_search(
            memory_search=memory_search,
            message=message,
            # ADR-091 (ревизия 2026-08-25): гейт ТОЛЬКО инстансный. Персональной настройки больше
            # нет — память работает везде, где RAG включён оператором.
            memory_enabled=self._settings.memory_enabled,
        ):
            explicit_only = await self._explicit_memory_block(
                user_id=user_id,
                memory_search_scope=memory_search_scope,
                workspace_project_id=workspace_project_id,
            )
            return explicit_only

        scope = memory_search_scope if memory_search_scope in ("global", "workspace") else "global"
        ws_filter = workspace_project_id if scope == "workspace" else None
        hits = await self._retriever.hybrid_search(
            user_id=user_id,
            query=message,
            workspace_project_id=ws_filter,
            scope=scope,
            exclude_session_id=exclude_session_id,
        )
        rag_block = self._retriever.format_retrieval_block(
            hits, max_chars=self._settings.memory_retrieval_max_chars
        )
        explicit_block = await self._explicit_memory_block(
            user_id=user_id,
            memory_search_scope=memory_search_scope,
            workspace_project_id=workspace_project_id,
        )
        if rag_block and explicit_block:
            return explicit_block + "\n\n" + rag_block
        return rag_block or explicit_block

    async def _explicit_memory_block(
        self,
        *,
        user_id: uuid.UUID,
        memory_search_scope: str,
        workspace_project_id: uuid.UUID | None,
    ) -> str | None:
        if not self._settings.memory_enabled:
            return None
        global_rows = await self._repo.list_memories(user_id, scope="global")
        workspace_rows: list[MemoryRow] = []
        if memory_search_scope == "workspace" and workspace_project_id is not None:
            workspace_rows = await self._repo.list_memories(
                user_id, scope="workspace", workspace_project_id=workspace_project_id
            )
        facts = [row.content for row in global_rows] + [row.content for row in workspace_rows]
        return self._retriever.format_explicit_memories(
            facts, max_chars=self._settings.memory_explicit_max_chars
        )

    async def list_memories(self, user_id: uuid.UUID) -> list[MemoryRow]:
        return await self._repo.list_all_memories(user_id)

    async def create_memory(
        self,
        user_id: uuid.UUID,
        *,
        content: str,
        workspace_project_id: uuid.UUID | None,
    ) -> MemoryRow:
        text = content.strip()
        if not text:
            raise ValidationFailedError("content must not be empty")
        if len(text) > self._settings.memory_explicit_entry_max_chars:
            raise ValidationFailedError("content exceeds size limit")
        if (
            workspace_project_id is not None
            and self._workspaces is not None
            and not await self._workspaces.owns_workspace(workspace_project_id, user_id)
        ):
            raise NotFoundError("workspace not found")
        row = await self._repo.create_memory(
            user_id=user_id,
            content=text,
            workspace_project_id=workspace_project_id,
        )
        await self._session.commit()
        return row

    async def delete_memory(self, user_id: uuid.UUID, memory_id: uuid.UUID) -> None:
        deleted = await self._repo.delete_memory(memory_id, user_id)
        if not deleted:
            raise NotFoundError("memory not found")
        await self._session.commit()

    async def backfill(self, user_id: uuid.UUID, *, batch_size: int = 100) -> int:
        return await self._indexer.backfill_user(user_id, batch_size=batch_size)

    async def stats(self, user_id: uuid.UUID) -> dict[str, int]:
        raw = await self._repo.raw_backfill_stats(user_id)
        return {
            "chunks": int(raw["chunks"]),
            "memories": int(raw["memories"]),
            "unindexedSteps": int(raw["unindexedSteps"]),
        }
