"""admin_credentials — зашифрованный оверлей креденшлов инстанса (ADR-116 §2.2).

Expand-only: одна НОВАЯ таблица, ни одна существующая не меняется, DML по существующим строкам
нет, засева и backfill нет. Пустая таблица = поведение до выката бит-в-бит: каждый креденшл
берётся из `.env`, как и раньше.

Значение хранится по схеме BYOK (envelope encryption, ADR-003): ``encrypted_value`` — nonce и
шифротекст AES-256-GCM под одноразовым DEK, ``encrypted_dek`` — DEK, обёрнутый KMS-клиентом.
Мастер-ключ остаётся в `.env` и в БД не попадает. Пустое значение хранится как зашифрованная
пустая строка: «явно выключено» отличается от «не задано» наличием строки.

Откат — ``DROP TABLE``: прежний код таблицу не читает.

Chain: … -> 0039_media_asset_store -> 0040_admin_credentials (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0040_admin_credentials
Revises: 0039_media_asset_store
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_admin_credentials"
down_revision: str | None = "0039_media_asset_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_credentials",
        sa.Column("credential_id", sa.Text(), primary_key=True),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_dek", sa.LargeBinary(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("admin_credentials")
