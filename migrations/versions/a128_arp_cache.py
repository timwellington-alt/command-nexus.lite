"""ARP cache populated by polling the L3 device (Core) directly.

LibreNMS does ARP polling for devices it manages, but the Core is
is_manual=true (no SNMP) so its ARP table never reaches LibreNMS.
Without it, the only mac↔ip mapping we have is DHCP — and statically-
assigned devices like the HALO sensors on VLAN 88 never go through
DHCP, so they have no IP in any Nexus table.

This table is the smallest fix: a poller telnets to the Core every
30 min, parses `show arp`, and upserts (mac, ip) here. The port
classifier joins on mac to enrich its labels with the IP.

Revision ID: a128_arp_cache
Revises: a127_oui_prefix_varchar
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a128_arp_cache"
down_revision = "a127_oui_prefix_varchar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "arp_cache",
        sa.Column("mac", sa.String(17), primary_key=True),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("vlan", sa.Integer, nullable=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_arp_cache_ip", "arp_cache", ["ip"])


def downgrade() -> None:
    op.drop_index("ix_arp_cache_ip", table_name="arp_cache")
    op.drop_table("arp_cache")
