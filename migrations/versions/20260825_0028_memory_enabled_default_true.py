"""Кросс-чатовая память включена по умолчанию.

Меняет `server_default` колонки на `true` И переключает СУЩЕСТВУЮЩИЕ строки. Без второй части
правка не подействовала бы ни на одного действующего пользователя: у них строка настроек уже
создана со значением `false`, и новый дефолт колонки к ней не применяется.

Данные при этом не меняются: чанки и факты писались всем пользователям и до этой миграции
(независимо от настройки) — включается ЧТЕНИЕ уже собранного, а не сбор нового.

Revision ID: 0028_memory_enabled_default_true
Revises: 0027_chat_documents
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028_memory_enabled_default_true"
down_revision: str | None = "0027_chat_documents"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column(
        "user_preferences",
        "memory_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("true"),
    )
    op.execute("UPDATE user_preferences SET memory_enabled = true WHERE memory_enabled = false")


def downgrade() -> None:
    # Обратный ход возвращает ТОЛЬКО дефолт: какие строки были выключены до выката, миграция не
    # знает, а массовое выключение всех обнулило бы и осознанный выбор пользователя.
    op.alter_column(
        "user_preferences",
        "memory_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("false"),
    )
