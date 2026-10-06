"""Manual room ↔ device link table.

Adds `room_device_links` so operators can pin an AP / phone / projector
to a room when the auto-match layer doesn't have a hook. Auto-match
still runs (AP name regex, phone extension-digit convention,
projector_cache building/room columns); a manual link supplements or
overrides those results.

`device_ref` is the natural key for the underlying cache row:
  - ap        → wireless_ap_cache.name
  - phone     → phone_extension_cache.extension
  - projector → projector_cache.name

Unique on (device_type, device_ref) — a single device can only be
linked to one room manually. Moving the link is a DELETE + POST.

Revision ID: a189_room_device_links
Revises: a188_room_assets_desktop
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a189_room_device_links"
down_revision = "a188_room_assets_desktop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "room_device_links",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("room_id", sa.Integer,
                  sa.ForeignKey("facility_rooms.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("device_type", sa.String(30), nullable=False),
        sa.Column("device_ref", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index(
        "ux_room_device_links_dev",
        "room_device_links",
        ["device_type", "device_ref"],
        unique=True,
    )
    op.create_index(
        "ix_room_device_links_room",
        "room_device_links",
        ["room_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_room_device_links_room", table_name="room_device_links")
    op.drop_index("ux_room_device_links_dev", table_name="room_device_links")
    op.drop_table("room_device_links")
