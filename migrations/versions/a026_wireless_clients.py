"""Add wireless client + AP cache + history tables for AP tracking.

Creates the three tables the wireless_client_job populates every
2 minutes from the Aruba Instant controllers, plus the history
table the chromebook location lookup and roster student detail
read from.

Uses ``IF NOT EXISTS`` on every create so re-running this migration
against the existing production DB (where these tables were created
ad-hoc) is a no-op rather than an error.

Revision ID: a026_wireless_clients
Revises: a025_vlan_map
Create Date: 2026-04-06
"""

from typing import Sequence, Union
from alembic import op

revision: str = "a026_wireless_clients"
down_revision: Union[str, None] = "a025_vlan_map"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wireless_client_cache (
            id SERIAL PRIMARY KEY,
            mac VARCHAR(20) NOT NULL,
            ip VARCHAR(50),
            ap_name VARCHAR(100),
            ssid VARCHAR(100),
            os_type VARCHAR(50),
            signal VARCHAR(20),
            speed VARCHAR(20),
            cached_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_wireless_client_cache_mac ON wireless_client_cache (mac)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wireless_client_cache_ip ON wireless_client_cache (ip)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wireless_ap_cache (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            ip VARCHAR(50),
            mac VARCHAR(20),
            model VARCHAR(50),
            status VARCHAR(20) DEFAULT 'up',
            client_count INTEGER DEFAULT 0,
            channel VARCHAR(20),
            power VARCHAR(20),
            building VARCHAR(20),
            cached_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_wireless_ap_cache_name ON wireless_ap_cache (name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wireless_ap_cache_building ON wireless_ap_cache (building)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wireless_client_history (
            id SERIAL PRIMARY KEY,
            mac VARCHAR(20) NOT NULL,
            ip VARCHAR(50),
            ap_name VARCHAR(100),
            ssid VARCHAR(100),
            first_seen TIMESTAMPTZ DEFAULT NOW(),
            last_seen TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_wireless_client_history_mac ON wireless_client_history (mac)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_wireless_client_history_mac_ap "
        "ON wireless_client_history (mac, ap_name)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wireless_client_history")
    op.execute("DROP TABLE IF EXISTS wireless_ap_cache")
    op.execute("DROP TABLE IF EXISTS wireless_client_cache")
