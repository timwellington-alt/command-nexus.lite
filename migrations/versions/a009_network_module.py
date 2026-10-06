"""Network module tables — device cache, port notes, NOC services.

Revision ID: a009_network
Revises: a008_door_name_unique
Create Date: 2026-03-28
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a009_network"
down_revision: Union[str, None] = "a008_door_name_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── network_device_cache ──────────────────────────────────────────
    op.create_table(
        "network_device_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("sysname", sa.String(255), nullable=False),
        sa.Column("ip", sa.String(50), nullable=False),
        sa.Column("os", sa.String(50), nullable=True),
        sa.Column("device_type", sa.String(50), nullable=True),
        sa.Column("hardware", sa.String(100), nullable=True),
        sa.Column("version", sa.String(100), nullable=True),
        sa.Column("status", sa.Integer(), server_default="0", nullable=False),
        sa.Column("uptime", sa.Integer(), server_default="0", nullable=False),
        sa.Column("building", sa.String(50), nullable=True),
        sa.Column("last_polled", sa.String(50), nullable=True),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id"),
    )
    op.create_index("ix_network_device_cache_device_id", "network_device_cache", ["device_id"])
    op.create_index("ix_network_device_cache_building", "network_device_cache", ["building"])

    # ── port_notes ────────────────────────────────────────────────────
    op.create_table(
        "port_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("port_name", sa.String(100), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_port_notes_device_id", "port_notes", ["device_id"])

    # ── noc_services ──────────────────────────────────────────────────
    op.create_table(
        "noc_services",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("check_type", sa.String(20), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("noc_services")
    op.drop_table("port_notes")
    op.drop_table("network_device_cache")
