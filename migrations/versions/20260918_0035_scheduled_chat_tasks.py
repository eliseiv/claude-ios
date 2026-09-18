"""scheduled_chat_tasks — one-shot delayed chat runs (ADR-107)

Creates ``scheduled_chat_tasks`` (modules/scheduled-chats/04-data-model.md): owner-scoped
one-shot queue rows with planned ``run_at``, optional planned ``session_id`` (UUID WITHOUT FK —
ON DELETE SET NULL would silently turn resume into «new session»), claim/lifecycle timestamps,
result ids and push idempotency.

``status`` / ``mode`` are TEXT + CHECK (same pattern as ``media_jobs``): extend without
``ALTER TYPE``. Expand-only: one CREATE TABLE + two indexes, no changes to existing tables.

Chain: … -> 0034_chat_steps_usage_null -> 0035_scheduled_chat_tasks (single head).

NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0035_scheduled_chat_tasks
Revises: 0034_chat_steps_usage_null
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_scheduled_chat_tasks"
down_revision: str | None = "0034_chat_steps_usage_null"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_chat_tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Planned session UUID: NO FK / NO ON DELETE SET NULL (ADR-107 §1).
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("assistant_mode", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("generation_mode", sa.Text(), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_message_step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("push_sent_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('scheduled', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_scheduled_chat_status",
        ),
        sa.CheckConstraint("mode IN ('credits', 'byok')", name="ck_scheduled_chat_mode"),
        sa.CheckConstraint(
            "assistant_mode IS NULL OR assistant_mode IN ('chat', 'code')",
            name="ck_scheduled_chat_assistant_mode",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_chat_status_run_at",
        "scheduled_chat_tasks",
        ["status", "run_at"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_chat_user_created",
        "scheduled_chat_tasks",
        ["user_id", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_chat_user_created", table_name="scheduled_chat_tasks")
    op.drop_index("ix_scheduled_chat_status_run_at", table_name="scheduled_chat_tasks")
    op.drop_table("scheduled_chat_tasks")
