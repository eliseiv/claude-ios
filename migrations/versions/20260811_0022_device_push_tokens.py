"""device_push_tokens + media_jobs.push_sent_at (ADR-067)

Registers APNs device tokens for push delivery and adds an idempotency stamp on
media_jobs so a completed generation emits at most one push (poll + reconciler race).

Also adds a partial index for the background media reconciler (Q-060-2 / ADR-067).

Chain: … -> 0021_media_templates -> 0022_device_push_tokens (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0022_device_push_tokens
Revises: 0021_media_templates
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0022_device_push_tokens"
down_revision: str | None = "0021_media_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_push_tokens",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("push_token", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False, server_default="ios"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "device_id", name="ux_push_tokens_user_device"),
    )
    op.create_index("ix_push_tokens_user", "device_push_tokens", ["user_id"])

    op.add_column(
        "media_jobs",
        sa.Column("push_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_media_jobs_non_terminal",
        "media_jobs",
        ["created_at"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_media_jobs_non_terminal", table_name="media_jobs")
    op.drop_column("media_jobs", "push_sent_at")
    op.drop_index("ix_push_tokens_user", table_name="device_push_tokens")
    # UniqueConstraint creates a constraint (not a standalone index); drop the constraint.
    op.drop_constraint("ux_push_tokens_user_device", "device_push_tokens", type_="unique")
    op.drop_table("device_push_tokens")
