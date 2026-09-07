"""user_preferences.default_voice_id — голос озвучки по умолчанию (ADR-100).

Одна колонка, expand-only: `ALTER TABLE user_preferences ADD COLUMN default_voice_id TEXT` —
nullable, без `server_default`, без backfill и без индекса. Существующие строки остаются `NULL`
(«голос инстанса», `TTS_DEFAULT_VOICE_ID`) и ведут себя как прежде.

Внешнего ключа НЕТ: реестр голосов живёт в коде (`src/app/chat/voices.py`), ровно как у `model`
(ADR-034) и `character_id` (ADR-097), — ссылочная целостность держится валидацией на
`PATCH /v1/preferences` (`422 unknown_voice` / `422 voice_output_disabled`), а не БД. Индекса нет:
по голосу не фильтруют и не сортируют, строка читается по первичному ключу.

Откат — `DROP COLUMN`.

Номер. ADR-100 §Последствия называет эту миграцию `0033` после `0032_admin_economics` (ADR-099) и
там же фиксирует, ЧТО из этого нормативно: «инвариант — **single head**, а не конкретный номер:
если она выкатывается раньше, номер и `down_revision` пересчитываются, иначе получаются две
головы». `0032_admin_economics` в репозитории на 2026-09-08 отсутствует (фактический head —
`0031_chat_character`, проверено `alembic heads`), поэтому пересчёт применён: эта ревизия занимает
`0032` и цепляется за фактическую голову. Таблицы двух миграций не пересекаются, порядок их
применения на данные не влияет.

Chain: … -> 0031_chat_character -> 0032_user_default_voice (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0032_user_default_voice
Revises: 0031_chat_character
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_user_default_voice"
down_revision: str | None = "0031_chat_character"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column("default_voice_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_preferences", "default_voice_id")
