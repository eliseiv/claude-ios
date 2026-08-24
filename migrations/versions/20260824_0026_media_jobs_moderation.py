"""media_jobs.moderation — вердикт модерации UGC (ADR-086 §10).

Expand-only: колонка nullable, бэкфилла нет. NULL = «не проверялось» и отдаётся клиенту как
status="unchecked" (ADR-086 §8) — ранее созданные задачи не объявляются прошедшими проверку.

Revision ID: 0026_media_jobs_moderation
Revises: 0025_memory_rag
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0026_media_jobs_moderation"
down_revision: str | None = "0025_memory_rag"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("media_jobs", sa.Column("moderation", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_jobs", "moderation")
