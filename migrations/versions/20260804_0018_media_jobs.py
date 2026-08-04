"""media_jobs — image/video generation runs submitted to the fal.ai queue (ADR-060)

Creates the ``media_jobs`` table (media-generation/04-data-model.md): one row per accepted
generation run, holding the fal queue handle (``fal_request_id`` + the two upstream polling URLs),
the debited credit amount and the normalized result. ``user_id`` is a FK to
``users(id) ON DELETE CASCADE``; the composite index on ``(user_id, created_at)`` backs the
owner-scoped newest-first listing of ``GET /v1/media/jobs``.

``status`` is plain TEXT with a CHECK constraint rather than a new PostgreSQL enum: the value set
tracks the fal queue lifecycle (an upstream contract), so adding a state must not require a
``CREATE TYPE`` migration on every instance.

Expand-only: one CREATE TABLE + one CREATE INDEX, no backfill, no changes to existing tables
(``users``/``wallets``/``ledger`` are reused as-is — the credit debit goes through the existing
WalletService and lands in ``ledger``).

Chain: 0001 -> ... -> 0017 -> 0018 (single head). down_revision is the FULL revision id of 0017
(``0017_subscription_will_renew``).

NOTE: the ``revision`` id MUST stay <= 32 chars — Alembic's ``alembic_version.version_num`` column
is VARCHAR(32).

Revision ID: 0018_media_jobs
Revises: 0017_subscription_will_renew
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_media_jobs"
down_revision: str | None = "0017_subscription_will_renew"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("fal_endpoint", sa.Text(), nullable=False),
        sa.Column("fal_request_id", sa.Text(), nullable=False),
        sa.Column("status_url", sa.Text(), nullable=False),
        sa.Column("response_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("credits_charged", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "credits_refunded", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_media_jobs_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_jobs_user_created", "media_jobs", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_media_jobs_user_created", table_name="media_jobs")
    op.drop_table("media_jobs")
