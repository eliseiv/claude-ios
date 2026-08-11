"""media_templates — gallery templates with BYTEA covers (ADR-066)

Catalog of photo/video generation templates for the iOS gallery: title, prompt, model,
parameters, required_input_images, and a cover image stored as BYTEA. Seeded with 5 image
+ 5 video placeholder rows (covers are a 1×1 PNG; operators replace via admin DELETE+POST).

Chain: … -> 0020_chat_temporary -> 0021_media_templates (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0021_media_templates
Revises: 0020_chat_temporary
Create Date: 2026-08-10
"""

from __future__ import annotations

import base64
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0021_media_templates"
down_revision: str | None = "0020_chat_temporary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 1×1 transparent PNG — valid cover placeholder until an operator uploads a real tile art.
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_SEED: list[dict[str, object]] = [
    # --- images (sort_order 10..50) ---
    {
        "id": "smart_resize",
        "kind": "image",
        "title": "Smart Resize",
        "prompt": (
            "Reframe and resize the photo for a clean social-media cover. Keep the subject "
            "sharp, balanced composition, natural colors."
        ),
        "model": "nano-banana-2",
        "required_input_images": 1,
        "parameters": {"aspectRatio": "16:9", "resolution": "1K", "numImages": 1},
        "sort_order": 10,
    },
    {
        "id": "bg_removal_change",
        "kind": "image",
        "title": "BG Removal & Change",
        "prompt": (
            "Remove the background of the person in the photo and place them on a soft "
            "studio backdrop with gentle lighting."
        ),
        "model": "nano-banana-2",
        "required_input_images": 1,
        "parameters": {"aspectRatio": "3:4", "resolution": "1K", "numImages": 1},
        "sort_order": 20,
    },
    {
        "id": "ecommerce_photos",
        "kind": "image",
        "title": "E-Commerce Photos",
        "prompt": (
            "Turn the product photo into a polished e-commerce lifestyle shot: clean surface, "
            "soft shadows, catalog-ready lighting."
        ),
        "model": "nano-banana-2",
        "required_input_images": 1,
        "parameters": {"aspectRatio": "1:1", "resolution": "2K", "numImages": 1},
        "sort_order": 30,
    },
    {
        "id": "photo_collage",
        "kind": "image",
        "title": "Photo Collage",
        "prompt": (
            "Arrange the provided photos into a stylish Polaroid-style collage on a light "
            "background, consistent spacing and soft shadows."
        ),
        "model": "nano-banana-2",
        "required_input_images": 2,
        "parameters": {"aspectRatio": "4:5", "resolution": "1K", "numImages": 1},
        "sort_order": 40,
    },
    {
        "id": "profile_picture",
        "kind": "image",
        "title": "Profile Picture",
        "prompt": (
            "Create a professional profile portrait from the photo: soft key light, clean "
            "background, natural skin tones, head-and-shoulders crop."
        ),
        "model": "nano-banana-2",
        "required_input_images": 1,
        "parameters": {"aspectRatio": "1:1", "resolution": "1K", "numImages": 1},
        "sort_order": 50,
    },
    # --- videos (sort_order 110..150) ---
    {
        "id": "product_hero_video",
        "kind": "video",
        "title": "Hero Product Reveal",
        "prompt": (
            "Cinematic product hero reveal: slow push-in on a luxury object, soft rim light, "
            "shallow depth of field, premium commercial look."
        ),
        "model": "kling-video",
        "required_input_images": 0,
        "parameters": {"aspectRatio": "9:16", "duration": "5"},
        "sort_order": 110,
    },
    {
        "id": "portrait_motion",
        "kind": "video",
        "title": "Portrait Motion",
        "prompt": (
            "Subtle cinematic motion from the portrait photo: gentle camera drift, natural "
            "hair and fabric movement, soft bokeh."
        ),
        "model": "kling-video",
        "required_input_images": 1,
        "parameters": {"duration": "5"},
        "sort_order": 120,
    },
    {
        "id": "lifestyle_reel",
        "kind": "video",
        "title": "Lifestyle Reel",
        "prompt": (
            "Short lifestyle reel: warm morning light, handheld micro-movements, inviting "
            "atmosphere for social stories."
        ),
        "model": "kling-video-v3",
        "required_input_images": 0,
        "parameters": {"aspectRatio": "9:16", "duration": "5", "generateAudio": False},
        "sort_order": 130,
    },
    {
        "id": "before_after_reveal",
        "kind": "video",
        "title": "Before & After Reveal",
        "prompt": (
            "Smooth before-and-after style reveal from the reference photo: elegant wipe "
            "transition feel, polished commercial grade."
        ),
        "model": "kling-video",
        "required_input_images": 1,
        "parameters": {"duration": "5"},
        "sort_order": 140,
    },
    {
        "id": "cinematic_broll",
        "kind": "video",
        "title": "Cinematic B-Roll",
        "prompt": (
            "Cinematic b-roll establishing shot: golden hour, slow pan, rich color grade, "
            "film-like grain."
        ),
        "model": "veo-3.1",
        "required_input_images": 0,
        "parameters": {
            "aspectRatio": "16:9",
            "duration": "4s",
            "resolution": "720p",
            "generateAudio": False,
        },
        "sort_order": 150,
    },
]


def upgrade() -> None:
    op.create_table(
        "media_templates",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("required_input_images", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parameters", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("cover_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("cover_media_type", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
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
        sa.CheckConstraint("kind IN ('image', 'video')", name="ck_media_templates_kind"),
        sa.CheckConstraint(
            "required_input_images >= 0 AND required_input_images <= 14",
            name="ck_media_templates_required_input_images",
        ),
        sa.CheckConstraint(
            "cover_media_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_media_templates_cover_media_type",
        ),
    )
    op.create_index(
        "ix_media_templates_kind_sort",
        "media_templates",
        ["kind", "sort_order"],
    )

    templates = sa.table(
        "media_templates",
        sa.column("id", sa.Text),
        sa.column("kind", sa.Text),
        sa.column("title", sa.Text),
        sa.column("prompt", sa.Text),
        sa.column("model", sa.Text),
        sa.column("required_input_images", sa.Integer),
        sa.column("parameters", JSONB),
        sa.column("cover_bytes", sa.LargeBinary),
        sa.column("cover_media_type", sa.Text),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        templates,
        [
            {
                **row,
                "cover_bytes": _PLACEHOLDER_PNG,
                "cover_media_type": "image/png",
            }
            for row in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_media_templates_kind_sort", table_name="media_templates")
    op.drop_table("media_templates")
