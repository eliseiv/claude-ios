"""Exact fal cost per media run (ADR-079).

Revision ID: 0024_media_jobs_provider_cost
Revises: 0023_request_logs
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_media_jobs_provider_cost"
down_revision: str | None = "0023_request_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no backfill on purpose: the values that move fal's bill (resolution,
    # duration, audio, image count) were never persisted, so existing rows have no exact cost
    # to write here. They stay NULL and the CRM recovers them from `credits_charged` instead,
    # marking what it cannot pin down exactly (ADR-079 §2).
    op.add_column(
        "media_jobs",
        sa.Column("provider_cost_usd", sa.Numeric(12, 6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_jobs", "provider_cost_usd")
