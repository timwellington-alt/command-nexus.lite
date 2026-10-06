"""Persist per-port MAC + LLDP neighbor data so network search can locate
a device by MAC address. Populated daily by a fabric scan worker.

Revision ID: a117_switch_port_endpoints
Revises: a116_transportation_extra_fields
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a117_switch_port_endpoints"
down_revision = "a116_transportation_extra_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "switch_port_endpoints",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.Integer, nullable=False),  # LibreNMS device_id
        sa.Column("port_id", sa.String(20), nullable=False),
        sa.Column("mac", sa.String(17), nullable=False),     # canonical colon-hex
        sa.Column("source", sa.String(10), nullable=False),  # 'lldp' | 'mac'
        sa.Column("vlan", sa.Integer, nullable=True),
        sa.Column("sysname", sa.String(255), nullable=True),
        sa.Column("port_descr", sa.String(255), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("device_id", "port_id", "mac", "source", name="uq_switch_port_endpoints_dpms"),
    )
    op.create_index("ix_switch_port_endpoints_mac", "switch_port_endpoints", ["mac"])
    op.create_index("ix_switch_port_endpoints_last_seen", "switch_port_endpoints", ["last_seen"])


def downgrade() -> None:
    op.drop_index("ix_switch_port_endpoints_last_seen", table_name="switch_port_endpoints")
    op.drop_index("ix_switch_port_endpoints_mac", table_name="switch_port_endpoints")
    op.drop_table("switch_port_endpoints")
