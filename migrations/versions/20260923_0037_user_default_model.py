"""user_preferences.default_model — дефолтная модель чата (preferences).

Одна колонка, expand-only: `ALTER TABLE user_preferences ADD COLUMN default_model TEXT` —
nullable, без `server_default`, без backfill и без индекса. Существующие строки остаются `NULL`
(«дефолт инстанса», как отдаёт `GET /v1/models` `default:true`) и ведут себя как прежде.

Внешнего ключа НЕТ: каталог моделей живёт в коде/оверлеях (`app.instance_config`), ровно как у
`model` в чате (ADR-034), `default_voice_id` (ADR-100) и `character_id` (ADR-097) — ссылочная
целостность держится валидацией на `PATCH /v1/preferences` (`422 unsupported_model`), а не БД.
Индекса нет: по модели не фильтруют и не сортируют, строка читается по первичному ключу.

Откат — `DROP COLUMN`.

Chain: … -> 0036_scheduled_chat_tasks -> 0037_user_default_model (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0037_user_default_model
Revises: 0036_scheduled_chat_tasks
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_user_default_model"
down_revision: str | None = "0036_scheduled_chat_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column("default_model", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_preferences", "default_model")
