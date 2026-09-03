"""legacy_user_ids: соответствие идентификаторов прежнего сервиса нашим (ADR-096).

Revision ID: 0030_legacy_user_ids
Revises: 0029_steps_created_idx
Create Date: 2026-09-03

Перенос с сервиса 232 выдал людям НОВЫЕ внутренние идентификаторы, а платёжный поставщик
продолжает присылать СТАРЫЕ: старое приложение живо, платежи создаются им, а вебхук настроен уже
на новый сервис. Разрешение пользователя знало два пути — `users.id` и `auth_devices.device_id` —
и оба промахивались, поэтому оплата отбрасывалась как `user_not_found`: деньги списаны, начисления
нет. Эта таблица даёт третий путь.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_legacy_user_ids"
down_revision: str | None = "0029_steps_created_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legacy_user_ids",
        # Идентификатор прежнего сервиса — ключ: он и приходит в вебхуке, по нему и ищем.
        sa.Column("legacy_user_id", sa.UUID(as_uuid=True), primary_key=True),
        # ON DELETE CASCADE: удалённый пользователь не должен оставлять запись, по которой
        # чужая оплата разрешится в несуществующего владельца.
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Откуда взято соответствие — чтобы происхождение строки было видно без раскопок.
        sa.Column("source", sa.Text(), nullable=False, server_default="migration"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Обратный поиск «кто из старых соответствует этому нашему» — для разбора обращений.
    op.create_index("ix_legacy_user_ids_user_id", "legacy_user_ids", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_legacy_user_ids_user_id", table_name="legacy_user_ids")
    op.drop_table("legacy_user_ids")
