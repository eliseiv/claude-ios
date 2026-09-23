"""media_jobs: provider / vendor_price / pending_result — generation via the proxy (ADR-108 §9).

Expand-only, three columns, NO DML over existing rows:

* ``provider TEXT NOT NULL DEFAULT ''`` — the proxy service that accepted the run; ``''`` marks a
  job of the direct fal client. A constant default fills the existing rows with ``''`` without an
  ``UPDATE`` (PostgreSQL 11+ stores it in the catalog), and that value is correct for them: every
  row created before this migration IS a direct-fal (legacy) job.
* ``vendor_price NUMERIC(18,6) NULL`` — actual vendor price reported by the callback.
* ``pending_result JSONB NULL`` — a ``completed`` callback result not yet applied by the shared
  completion path.

No constraint or index of an existing column changes; no ``CHECK`` on ``provider`` (the set of
proxy services is an external contract, like ``status``). The webhook token is not stored.

Rollback — ``DROP COLUMN`` ×3 (old code ignores the new columns, ADR-108 §Порядок выката п.5).

Chain: … -> 0037_user_default_model -> 0038_media_jobs_proxy (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0038_media_jobs_proxy
Revises: 0037_user_default_model
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0038_media_jobs_proxy"
down_revision: str | None = "0037_user_default_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "media_jobs",
        sa.Column("provider", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "media_jobs",
        sa.Column("vendor_price", sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        "media_jobs",
        sa.Column("pending_result", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_jobs", "pending_result")
    op.drop_column("media_jobs", "vendor_price")
    op.drop_column("media_jobs", "provider")
