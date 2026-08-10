"""chat_sessions.is_temporary — hide temporary v2 chats from GET /v1/chats

Adds a session-fixed boolean written only at create via ``POST /v1/chat/v2/run``
(``temporary: true``). Temporary sessions stay addressable by id for multi-turn and
``DELETE /v1/chats/{id}``, but are excluded from the list query. Partial index on
``(user_id, updated_at DESC) WHERE is_temporary = false`` keeps the list path cheap.

Expand-only: one ADD COLUMN + one CREATE INDEX. Existing rows default to false.

Chain: … -> 0019_media_edit_chain -> 0020_chat_temporary (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0020_chat_temporary
Revises: 0019_media_edit_chain
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_chat_temporary"
down_revision: str | None = "0019_media_edit_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "is_temporary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_sessions_user_non_temporary_updated",
        "chat_sessions",
        ["user_id", sa.text("updated_at DESC")],
        postgresql_where=sa.text("is_temporary = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_user_non_temporary_updated", table_name="chat_sessions")
    op.drop_column("chat_sessions", "is_temporary")
