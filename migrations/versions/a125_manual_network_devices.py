"""Manual network devices — devices Nexus must track that LibreNMS doesn't poll.

The 5406R-Core is the load-bearing example: every closet's uplink LLDPs
to it, but LibreNMS can't reach it (no SNMP enabled on the Core). Without
the row in network_device_cache, every LLDP-neighbor classifier and every
topology view that hops through the core breaks.

The refresh_device_cache job runs every 60s and does DELETE + INSERT from
LibreNMS — without is_manual it would wipe rows like this on the next tick.

Revision ID: a125_manual_network_devices
Revises: a124_port_device_overrides
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a125_manual_network_devices"
down_revision = "a124_port_device_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "network_device_cache",
        sa.Column("is_manual", sa.Boolean, server_default=sa.false(), nullable=False),
    )

    # Seed the HP 5406R-Core. device_id 9001 sits well above any LibreNMS-
    # assigned ID so manual entries can't ever collide with a real LibreNMS
    # row. Operators can add more manuals via the same id range (9000+).
    op.execute("""
        INSERT INTO network_device_cache
            (device_id, hostname, sysname, ip, os, device_type, hardware,
             version, status, uptime, building, last_polled, cached_at,
             alert_priority, is_manual)
        VALUES
            (9001, '192.0.2.1', 'example-switch', '192.0.2.1',
             'ProCurve', 'network', 'HP 5406R-Core',
             NULL, 1, 0, 'CO', now(), now(),
             'critical', TRUE)
        ON CONFLICT (device_id) DO UPDATE SET is_manual = TRUE
    """)


def downgrade() -> None:
    op.execute("DELETE FROM network_device_cache WHERE is_manual = TRUE")
    op.drop_column("network_device_cache", "is_manual")
