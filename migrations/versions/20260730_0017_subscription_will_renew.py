"""add subscriptions.will_renew (nullable) for auto-renew intent

Revision ID: 0017_subscription_will_renew
Revises: 0016_chat_provider_state
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_subscription_will_renew"
down_revision: str | None = "0016_chat_provider_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("will_renew", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "will_renew")
