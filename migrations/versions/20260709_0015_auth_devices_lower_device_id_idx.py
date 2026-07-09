"""auth_devices lower(device_id) functional index — case-insensitive deviceId resolve (ADR-055)

The payment-webhook user resolution (``billing_common/resolve.py``, ADR-055) matches an aggregator
deviceId against ``auth_devices`` case-insensitively: iOS stores ``identifierForVendor.uuidString``
in UPPERCASE while our resolver always compares a lowercase ``str(uuid.UUID)``. Without a functional
index on ``lower(device_id)`` that ``WHERE lower(device_id) = :x`` predicate cannot use the
``device_id`` PRIMARY KEY index and falls back to a seq scan. This adds
``ix_auth_devices_lower_device_id`` to back the lookup.

Non-unique on purpose: prod is currently collision-free (``lower(device_id)`` maps to a single
user everywhere, verified ADR-055), but a UNIQUE index would fail the migration should any legacy
casing duplicate ever exist. Expand-only: no data touched. Notably we do NOT rewrite
``device_id`` to lowercase — it is a ``TEXT`` PK with an inbound ``ON DELETE CASCADE`` FK and
authentication matches it by exact value (``auth/service.py``), so normalizing would detach existing
users from their accounts.

NOTE: the ``revision`` id MUST stay <= 32 chars — Alembic's ``alembic_version.version_num`` column
is VARCHAR(32); a longer id fails the UPDATE and breaks every migrate step. Hence the abbreviated
``0015_devid_lower_idx`` (20 chars).

Revision ID: 0015_devid_lower_idx
Revises: 0014_cp_webhook_events
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_devid_lower_idx"
down_revision: str | None = "0014_cp_webhook_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_auth_devices_lower_device_id "
        "ON auth_devices (lower(device_id))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_auth_devices_lower_device_id")
