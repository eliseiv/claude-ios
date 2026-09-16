"""Avatar speech, reusable user avatars and makeup presets.

The migration is expand-only: existing media job rows receive server defaults that preserve the
old feed and polling behaviour, while both new catalogs start empty.  Features are additionally
disabled by default in application settings, so deploying this schema to every backend instance
does not expose or activate a partially configured product.

Revision ID: 0035_media_features
Revises: 0034_chat_steps_usage_null
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0035_media_features"
down_revision: str | None = "0034_chat_steps_usage_null"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_feature_presets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("feature", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("gender", sa.Text(), nullable=True),
        sa.Column("style", sa.Text(), nullable=True),
        sa.Column("provider_value", sa.Text(), nullable=True),
        sa.Column("image_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("image_media_type", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "feature IN ('avatar', 'background', 'makeup')",
            name="ck_media_feature_presets_feature",
        ),
        sa.CheckConstraint(
            "gender IS NULL OR gender IN ('male', 'female')",
            name="ck_media_feature_presets_gender",
        ),
        sa.CheckConstraint(
            "image_media_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_media_feature_presets_media_type",
        ),
    )
    op.create_index(
        "ix_media_feature_presets_list",
        "media_feature_presets",
        ["feature", "is_active", "sort_order", "id"],
    )

    op.create_table(
        "user_avatars",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "source_preset_id",
            sa.Text(),
            sa.ForeignKey("media_feature_presets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("original_image_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("original_media_type", sa.Text(), nullable=False),
        sa.Column("prepared_image_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("prepared_media_type", sa.Text(), nullable=True),
        sa.Column("background_image_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("background_media_type", sa.Text(), nullable=True),
        sa.Column("background_color", sa.Text(), nullable=True),
        sa.Column("preparation_job_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "original_media_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_user_avatars_original_media_type",
        ),
        sa.CheckConstraint(
            "prepared_media_type IS NULL OR "
            "prepared_media_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_user_avatars_prepared_media_type",
        ),
        sa.CheckConstraint(
            "background_media_type IS NULL OR "
            "background_media_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_user_avatars_background_media_type",
        ),
    )
    op.create_index("ix_user_avatars_user_created", "user_avatars", ["user_id", "created_at"])

    op.add_column(
        "media_jobs",
        sa.Column("operation", sa.Text(), nullable=False, server_default=sa.text("'generation'")),
    )
    op.add_column("media_jobs", sa.Column("operation_input", JSONB(), nullable=True))
    op.add_column(
        "media_jobs",
        sa.Column(
            "visible_in_history", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )


def downgrade() -> None:
    op.drop_column("media_jobs", "visible_in_history")
    op.drop_column("media_jobs", "operation_input")
    op.drop_column("media_jobs", "operation")
    op.drop_index("ix_user_avatars_user_created", table_name="user_avatars")
    op.drop_table("user_avatars")
    op.drop_index("ix_media_feature_presets_list", table_name="media_feature_presets")
    op.drop_table("media_feature_presets")
