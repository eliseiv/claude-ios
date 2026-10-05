"""media_jobs.remaining_routes — маршруты прокси, ещё не опробованные для задачи (ADR-108 §4.4).

Expand-only: одна nullable-колонка, без DML и backfill. У строк до миграции ``NULL`` — колбэк
``failed`` для них терминален, как прежде. Откат — ``DROP COLUMN``: прежний код колонку не читает.

Chain: … -> 0040_admin_credentials -> 0041_media_jobs_remaining_routes (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0041_media_jobs_remaining_routes
Revises: 0040_admin_credentials
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0041_media_jobs_remaining_routes"
down_revision: str | None = "0040_admin_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("media_jobs", sa.Column("remaining_routes", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_jobs", "remaining_routes")
