"""chat_sessions provider_state and chat generation backend.

Revision ID: 0016_chat_provider_state
Revises: 0015_devid_lower_idx
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_chat_provider_state"
down_revision: str | None = "0015_devid_lower_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("provider_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("chat_sessions", sa.Column("generation_backend", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "generation_backend")
    op.drop_column("chat_sessions", "provider_state")
