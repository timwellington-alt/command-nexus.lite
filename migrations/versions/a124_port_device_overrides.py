"""Per-port device-kind override table.

When the layered classifier in port_device_kinds() picks the wrong kind
(e.g. an HP OUI that's actually a network printer, not a workstation),
the operator pins the correct value here. Manual overrides always win
over the auto-detect.

Revision ID: a124_port_device_overrides
Revises: a123_nmap_scans
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a124_port_device_overrides"
down_revision = "a123_nmap_scans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "port_device_overrides",
        sa.Column("device_id", sa.Integer, nullable=False),
        sa.Column("port_id", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("device_id", "port_id", name="pk_port_device_overrides"),
    )


def downgrade() -> None:
    op.drop_table("port_device_overrides")
