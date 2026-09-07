"""chat_sessions.character_id — персонаж чата (ADR-097).

Одна колонка, expand-only: `ALTER TABLE chat_sessions ADD COLUMN character_id TEXT` —
nullable, без `server_default`, без backfill и без индекса. Существующие строки остаются
`NULL` («чат без персонажа») и ведут себя как прежде.

Внешнего ключа НЕТ: реестр персонажей живёт в коде (`src/app/chat/characters.py`), ровно как
у `model` (ADR-034), — ссылочная целостность держится валидацией при создании сессии
(`422 unknown_character`), а не БД. Индекса нет: по персонажу не фильтруют и не сортируют, а
список чатов уже обслуживается `ix_sessions_user_pinned_updated`.

Откат — `DROP COLUMN`.

Chain: … -> 0030_legacy_user_ids -> 0031_chat_character (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0031_chat_character
Revises: 0030_legacy_user_ids
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_chat_character"
down_revision: str | None = "0030_legacy_user_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("character_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "character_id")
