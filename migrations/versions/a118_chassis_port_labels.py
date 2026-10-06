"""Translation table: chassis ifIndex/PortId -> friendly slot/letter label.

Populated by a periodic SSH harvest of access-switch LLDP brief tables.
LibreNMS's `links` view exposes the numeric PortId but not the friendly
PortDescr; this table fills the gap so the port detail panel can show
e.g. "example-switch port D19" instead of "port 115".

Revision ID: a118_chassis_port_labels
Revises: a117_switch_port_endpoints
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a118_chassis_port_labels"
down_revision = "a117_switch_port_endpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chassis_port_labels",
        sa.Column("chassis_sysname", sa.String(255), nullable=False),
        sa.Column("port_id", sa.String(40), nullable=False),
        sa.Column("friendly_port", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("chassis_sysname", "port_id", name="pk_chassis_port_labels"),
    )


def downgrade() -> None:
    op.drop_table("chassis_port_labels")
