"""CRM request history (ADR-077).

Revision ID: 0023_request_logs
Revises: 0022_device_push_tokens
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0023_request_logs"
down_revision: str | None = "0022_device_push_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "request_logs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("prompt_preview", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="started"),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default="202"),
        sa.Column("tokens_spent", sa.Numeric(18, 6), nullable=True),
        sa.Column("provider_cost_usd", sa.Float(), nullable=True),
        sa.Column("refunded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("message_step_id", UUID(as_uuid=True), nullable=True),
        sa.Column("media_job_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'queued', 'completed', 'failed')",
            name="ck_request_logs_status",
        ),
    )
    op.create_index(
        "ix_request_logs_user_started",
        "request_logs",
        ["user_id", sa.text("started_at DESC")],
    )
    op.create_index(
        "ux_request_logs_media_job",
        "request_logs",
        ["media_job_id"],
        unique=True,
        postgresql_where=sa.text("media_job_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_request_logs_media_job", table_name="request_logs")
    op.drop_index("ix_request_logs_user_started", table_name="request_logs")
    op.drop_table("request_logs")
