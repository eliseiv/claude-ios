"""media_jobs edit chain — parent_job_id + input_image_urls (ADR-063)

Adds the two columns the generations feed needs to show "this was made from that"
(media-generation/04-data-model.md):

* ``parent_job_id`` — the job this run was made from, set when the client submits ``sourceJobId``.
  Self-FK ``ON DELETE SET NULL``, not CASCADE: deleting a bad source frame means "take it out of
  the feed", not "erase everything that grew from it" (ADR-063 §2).
* ``input_image_urls`` — the reference-image URLs actually sent upstream. Persisted rather than
  derived from the parent, because the parent may be deleted and the feed still has to show what
  the run was made from.

Both are NULLable with no backfill: rows created before this migration genuinely have no parent
and no recorded input, and inventing one would be a lie about history.

Expand-only: two ADD COLUMN on an existing table, no data rewrite, no index changes (the feed's
keyset pagination orders by ``(created_at, id)``, already covered by ``ix_media_jobs_user_created``
from 0018).

Chain: 0001 -> ... -> 0018 -> 0019 (single head). down_revision is the FULL revision id of 0018.

NOTE: the ``revision`` id MUST stay <= 32 chars — Alembic's ``alembic_version.version_num`` column
is VARCHAR(32).

Revision ID: 0019_media_edit_chain
Revises: 0018_media_jobs
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_media_edit_chain"
down_revision: str | None = "0018_media_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "media_jobs",
        sa.Column("parent_job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "media_jobs",
        sa.Column("input_image_urls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_foreign_key(
        "fk_media_jobs_parent_job",
        "media_jobs",
        "media_jobs",
        ["parent_job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_media_jobs_parent_job", "media_jobs", type_="foreignkey")
    op.drop_column("media_jobs", "input_image_urls")
    op.drop_column("media_jobs", "parent_job_id")
