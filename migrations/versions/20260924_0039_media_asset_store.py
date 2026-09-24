"""media_jobs: state of our own 30-day copy of a generation result (ADR-109 §7).

Expand-only, seven columns and two partial indexes, NO DML over existing rows:

* ``asset_store_status TEXT NOT NULL DEFAULT ''`` with a ``CHECK`` over our own state domain
  ``('', 'pending', 'stored', 'failed', 'missing', 'expired')``. The constant default fills the
  existing rows with ``''`` without an ``UPDATE`` (PostgreSQL 11+ keeps it in the catalog), and
  that value is correct for them: storage was never applied to a row older than this migration.
* ``asset_store_attempts INTEGER NOT NULL DEFAULT 0``, ``asset_store_next_attempt_at``,
  ``assets_expire_at``, ``assets_stored_at`` (``TIMESTAMPTZ NULL``), ``assets_stored_bytes
  BIGINT NULL``, ``stored_assets JSONB NULL`` (ORM ``JSONB(none_as_null=True)``).

No backfill (Q-109-6): already completed jobs are not stored retroactively.

Indexes are created WITHOUT ``CONCURRENTLY``: migrations run before the application starts
(07-deployment.md §Миграции), both indexes are partial and their predicates are false on every
existing row, so the build is one sequential scan of ``media_jobs`` writing an empty index — the
same technique as ``ix_steps_created_at`` (0029) over the far larger ``chat_steps``.
``CONCURRENTLY`` would also be illegal inside the migration transaction.

Rollback — drop the two indexes, the CHECK and the seven columns (old code ignores them).

Chain: … -> 0038_media_jobs_proxy -> 0039_media_asset_store (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0039_media_asset_store
Revises: 0038_media_jobs_proxy
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0039_media_asset_store"
down_revision: str | None = "0038_media_jobs_proxy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STORE_STATES = "('', 'pending', 'stored', 'failed', 'missing', 'expired')"
_EXPIRE_STATES = "('stored', 'pending', 'failed', 'missing')"


def upgrade() -> None:
    op.add_column(
        "media_jobs",
        sa.Column("asset_store_status", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.create_check_constraint(
        "ck_media_jobs_asset_store_status",
        "media_jobs",
        f"asset_store_status IN {_STORE_STATES}",
    )
    op.add_column(
        "media_jobs",
        sa.Column(
            "asset_store_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "media_jobs",
        sa.Column("asset_store_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "media_jobs",
        sa.Column("assets_expire_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "media_jobs",
        sa.Column("assets_stored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "media_jobs",
        sa.Column("assets_stored_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "media_jobs",
        sa.Column("stored_assets", JSONB(), nullable=True),
    )
    op.create_index(
        "ix_media_jobs_asset_store_pending",
        "media_jobs",
        ["asset_store_next_attempt_at"],
        postgresql_where=sa.text("asset_store_status = 'pending'"),
    )
    op.create_index(
        "ix_media_jobs_assets_expire",
        "media_jobs",
        ["assets_expire_at"],
        postgresql_where=sa.text(f"asset_store_status IN {_EXPIRE_STATES}"),
    )


def downgrade() -> None:
    op.drop_index("ix_media_jobs_assets_expire", table_name="media_jobs")
    op.drop_index("ix_media_jobs_asset_store_pending", table_name="media_jobs")
    op.drop_column("media_jobs", "stored_assets")
    op.drop_column("media_jobs", "assets_stored_bytes")
    op.drop_column("media_jobs", "assets_stored_at")
    op.drop_column("media_jobs", "assets_expire_at")
    op.drop_column("media_jobs", "asset_store_next_attempt_at")
    op.drop_column("media_jobs", "asset_store_attempts")
    op.drop_constraint("ck_media_jobs_asset_store_status", "media_jobs", type_="check")
    op.drop_column("media_jobs", "asset_store_status")
