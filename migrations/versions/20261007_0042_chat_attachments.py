"""chat_attachments — байты вложений хода чата (ADR-120 §1).

Expand-only: новая таблица, бэкфилла нет (прежние вложения не восстановимы). CASCADE по
``user_id`` и ``session_id``; строки усечённых ходов удаляются кодом (FK к шагу нет).

Chain: … -> 0041_media_jobs_remaining_routes -> 0042_chat_attachments (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0042_chat_attachments
Revises: 0041_media_jobs_remaining_routes
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0042_chat_attachments"
down_revision: str | None = "0041_media_jobs_remaining_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_attachments",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_step_id", UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ux_chat_attachments_turn",
        "chat_attachments",
        ["session_id", "message_step_id", "position"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_chat_attachments_turn", table_name="chat_attachments")
    op.drop_table("chat_attachments")
