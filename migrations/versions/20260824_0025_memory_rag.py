"""Cross-chat RAG memory: pgvector chunks + explicit user memories.

Revision ID: 0025_memory_rag
Revises: 0024_media_jobs_provider_cost
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0025_memory_rag"
down_revision: str | None = "0024_media_jobs_provider_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "chat_chunks",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "session_id",
            sa.UUID(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_step_id", sa.UUID(), nullable=False),
        sa.Column("message_step_id", sa.UUID(), nullable=False),
        sa.Column("workspace_project_id", sa.UUID(), nullable=True),
        sa.Column("session_title", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(_EMBEDDING_DIM), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chat_step_id",
            "chunk_index",
            name="uq_chat_chunks_step_chunk",
        ),
    )
    op.create_index("ix_chat_chunks_user_created", "chat_chunks", ["user_id", "created_at"])
    op.create_index("ix_chat_chunks_session", "chat_chunks", ["session_id"])
    op.create_index(
        "ix_chat_chunks_workspace",
        "chat_chunks",
        ["user_id", "workspace_project_id"],
    )
    op.execute(
        """
        CREATE INDEX ix_chat_chunks_embedding_hnsw ON chat_chunks
        USING hnsw (embedding vector_cosine_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_chat_chunks_fts ON chat_chunks
        USING gin (to_tsvector('simple', text))
        """
    )

    op.create_table(
        "user_memories",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_project_id",
            sa.UUID(),
            sa.ForeignKey("workspace_projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'explicit'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_memories_user", "user_memories", ["user_id", "created_at"])

    op.add_column(
        "user_preferences",
        sa.Column(
            "memory_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "user_preferences",
        sa.Column(
            "memory_search_scope",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'global'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_preferences", "memory_search_scope")
    op.drop_column("user_preferences", "memory_enabled")
    op.drop_index("ix_user_memories_user", table_name="user_memories")
    op.drop_table("user_memories")
    op.execute("DROP INDEX IF EXISTS ix_chat_chunks_fts")
    op.execute("DROP INDEX IF EXISTS ix_chat_chunks_embedding_hnsw")
    op.drop_index("ix_chat_chunks_workspace", table_name="chat_chunks")
    op.drop_index("ix_chat_chunks_session", table_name="chat_chunks")
    op.drop_index("ix_chat_chunks_user_created", table_name="chat_chunks")
    op.drop_table("chat_chunks")
    op.execute("DROP EXTENSION IF EXISTS vector")
