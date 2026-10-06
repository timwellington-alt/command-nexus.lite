"""Per-Paxton-ACU live status cache.

Populated by the probe_paxton_acus worker every ~5 minutes via direct SSH
to the access switches that have Paxton MACs in their tables — bypasses
the daily LibreNMS sync chain (which itself reads stale FDB data) so the
Network page Access dots reflect what's actually on the wire right now.

Revision ID: a120_paxton_acu_status
Revises: a119_wireless_ap_extended
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a120_paxton_acu_status"
down_revision = "a119_wireless_ap_extended"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paxton_acu_status",
        sa.Column("mac", sa.String(17), primary_key=True),
        sa.Column("switch_device_id", sa.Integer, nullable=True, index=True),
        sa.Column("switch_port", sa.String(40), nullable=True),
        sa.Column("vlan", sa.Integer, nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("online", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probed", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("paxton_acu_status")
