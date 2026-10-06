"""HALO 2C integration — 3 tables.

halo_devices  — one row per HALO unit. Snapshot of current state +
                stable identity (mac, serial, firmware). Updated on
                each poll cycle; the row exists from first sighting.
halo_readings — append-only time series. One row per (device, metric,
                timestamp). Daily cleanup keeps 90 days.
halo_events   — webhook receiver log. Append-only. Every payload IPVideo
                pushes lands here with HMAC validation status. The router
                fans events out to the alert pipeline.

Revision ID: a129_halo_integration
Revises: a128_arp_cache
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "a129_halo_integration"
down_revision = "a128_arp_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "halo_devices",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("mac", sa.String(17), nullable=False, unique=True),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("sysname", sa.String(120), nullable=True),
        sa.Column("serial", sa.String(64), nullable=True),
        sa.Column("firmware", sa.String(64), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("location_label", sa.String(255), nullable=True),
        sa.Column("room", sa.String(120), nullable=True),
        sa.Column("building", sa.String(50), nullable=True),
        sa.Column("online", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_halo_devices_ip", "halo_devices", ["ip"])
    op.create_index("ix_halo_devices_building", "halo_devices", ["building"])

    op.create_table(
        "halo_readings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.Integer,
                  sa.ForeignKey("halo_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric", sa.String(40), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_halo_readings_device_time",
                    "halo_readings", ["device_id", "metric", "recorded_at"])

    op.create_table(
        "halo_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.Integer,
                  sa.ForeignKey("halo_devices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_ip", sa.String(45), nullable=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(20), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("hmac_ok", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("alerted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("alert_detail", sa.Text, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_halo_events_device_time",
                    "halo_events", ["device_id", "received_at"])
    op.create_index("ix_halo_events_type", "halo_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("halo_events")
    op.drop_table("halo_readings")
    op.drop_table("halo_devices")
