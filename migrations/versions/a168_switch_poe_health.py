"""Cache of PoE-supply health per switch, populated by a periodic
SSH probe. Feeds the /network overview status ring + the damage
scanner.

Why a table (vs. Redis / SNMP): the PoE bus dead state (0W available)
on HPE 2930F switches is invisible to SNMP — the PSU state sensor
still reports 'psPowered' because the PSU itself is fine; only the
downstream PoE circuitry is dead. We have to SSH the switch to run
``show power`` and parse ``Total Available Power``. Persisting the
result lets the fault ring and damage scanner both read cheaply
without a per-render SSH.

Revision ID: a168_switch_poe_health
Revises: a167_damage_reports
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a168_switch_poe_health"
down_revision = "a167_damage_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "switch_poe_health",
        sa.Column("device_id", sa.Integer, primary_key=True),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("sysname", sa.String(200)),
        # None when we couldn't reach the switch (SSH timeout, cert error,
        # etc.) — probe_error tells you why. Non-null means we successfully
        # parsed the reading (0 = supply dead, >0 = healthy).
        sa.Column("watts_available", sa.Integer),
        sa.Column("watts_drawn", sa.Integer),
        sa.Column("probe_error", sa.Text),
        sa.Column("last_probed_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_switch_poe_health_ip", "switch_poe_health", ["ip"])


def downgrade() -> None:
    op.drop_table("switch_poe_health")
