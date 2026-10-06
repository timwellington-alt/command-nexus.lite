"""Projector cache table for the PJLink poller.

Keyed on MAC (stable across IP changes — projector moving between
DHCP scopes keeps history). One row per physical unit. Daily poll
job populates everything except the discovery metadata.

Revision ID: a135_projector_cache
Revises: a134_printer_serial
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a135_projector_cache"
down_revision = "a134_printer_serial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projector_cache",
        sa.Column("mac", sa.String(17), primary_key=True),
        sa.Column("ip", sa.String(45)),
        sa.Column("name", sa.String(100)),
        sa.Column("manufacturer", sa.String(60)),
        sa.Column("model", sa.String(100)),
        sa.Column("serial", sa.String(60)),
        sa.Column("software_version", sa.String(60)),
        sa.Column("pjlink_class", sa.String(4)),
        sa.Column("power", sa.String(16)),           # on / standby / cooling / warmup / unknown
        sa.Column("input", sa.String(32)),           # decoded ("RGB 1")
        sa.Column("input_raw", sa.String(8)),        # raw ("11")
        sa.Column("lamp_hours", sa.Integer),
        sa.Column("lamp_lit", sa.Boolean),
        sa.Column("filter_hours", sa.Integer),       # PJLink Class 2 %2FILT — may be null
        sa.Column("errors_json", sa.JSON),           # {fan/lamp/temp/cover/filter/other → ok/warning/error}
        sa.Column("error_summary", sa.Text),         # human-readable rollup
        # Discovery metadata
        sa.Column("discovery_source", sa.String(20), nullable=False, server_default="arp"),
        sa.Column("building", sa.String(50)),        # parsed from name (e.g. PES-213-PRO → PES)
        sa.Column("room", sa.String(50)),            # parsed (213)
        # Status
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),      # last successful poll
        sa.Column("last_error", sa.Text),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("alert_priority", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("notes", sa.Text),
    )
    op.create_index("ix_projector_cache_ip", "projector_cache", ["ip"])
    op.create_index("ix_projector_cache_building", "projector_cache", ["building"])


def downgrade() -> None:
    op.drop_index("ix_projector_cache_building", "projector_cache")
    op.drop_index("ix_projector_cache_ip", "projector_cache")
    op.drop_table("projector_cache")
