"""Extend wireless_ap_cache with hardware, uplink and per-radio metrics.

Adds the fields needed for a full AP profile view: serial, firmware,
uptime, the switch+port the AP is plugged into (from LLDP), and per-radio
channel/power/noise/utilization/clients for 5 GHz (radio 0) and 2.4 GHz
(radio 1). Per-radio data is pulled from LibreNMS wireless_sensors keyed
by AP serial number; LLDP uplink is pulled from switch_port_endpoints.

Revision ID: a119_wireless_ap_extended
Revises: a118_chassis_port_labels
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a119_wireless_ap_extended"
down_revision = "a118_chassis_port_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("wireless_ap_cache") as b:
        # Hardware
        b.add_column(sa.Column("serial", sa.String(40), nullable=True))
        b.add_column(sa.Column("firmware", sa.String(60), nullable=True))
        b.add_column(sa.Column("uptime_seconds", sa.BigInteger, nullable=True))
        # Uplink (from LLDP)
        b.add_column(sa.Column("switch_neighbor", sa.String(255), nullable=True))
        b.add_column(sa.Column("switch_port", sa.String(40), nullable=True))
        # Per-radio 5 GHz (radio 0)
        b.add_column(sa.Column("radio_5g_channel", sa.String(20), nullable=True))
        b.add_column(sa.Column("radio_5g_power_dbm", sa.Float, nullable=True))
        b.add_column(sa.Column("radio_5g_noise_dbm", sa.Float, nullable=True))
        b.add_column(sa.Column("radio_5g_util_pct", sa.Float, nullable=True))
        b.add_column(sa.Column("radio_5g_clients", sa.Integer, nullable=True))
        # Per-radio 2.4 GHz (radio 1)
        b.add_column(sa.Column("radio_24g_channel", sa.String(20), nullable=True))
        b.add_column(sa.Column("radio_24g_power_dbm", sa.Float, nullable=True))
        b.add_column(sa.Column("radio_24g_noise_dbm", sa.Float, nullable=True))
        b.add_column(sa.Column("radio_24g_util_pct", sa.Float, nullable=True))
        b.add_column(sa.Column("radio_24g_clients", sa.Integer, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("wireless_ap_cache") as b:
        for col in (
            "serial", "firmware", "uptime_seconds",
            "switch_neighbor", "switch_port",
            "radio_5g_channel", "radio_5g_power_dbm", "radio_5g_noise_dbm",
            "radio_5g_util_pct", "radio_5g_clients",
            "radio_24g_channel", "radio_24g_power_dbm", "radio_24g_noise_dbm",
            "radio_24g_util_pct", "radio_24g_clients",
        ):
            b.drop_column(col)
