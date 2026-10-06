"""NOC service check history + alert bookkeeping.

Adds the columns the new check_noc_services scheduled worker needs to
persist results between runs + drive debounced alerting. Before this,
noc_services had no history at all — the NOC page ran the check on
render and forgot everything. Symptom: 192.0.2.1 DNS died and Nexus
never noticed because nobody was watching that page (2026-09-08).

Columns:
  last_checked_at        — when the last check ran
  last_ok                — most recent up/down verdict
  last_error             — error text on the last failure (null on ok)
  last_ms                — round-trip ms on last check
  last_rcode             — DNS-specific rcode name (NOERROR / SERVFAIL /
                           REFUSED / NXDOMAIN / …). Null for non-DNS.
  consecutive_failures   — count of back-to-back failed checks (used to
                           debounce alerts: fire only after N in a row)
  last_state_change_at   — when ok flipped. Powers "down for 12 min"
                           labels in the UI without another table.

Revision ID: a191_noc_services_monitoring
Revises: a190_id_card_status
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a191_noc_services_monitoring"
down_revision = "a190_id_card_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("noc_services", sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("noc_services", sa.Column("last_ok", sa.Boolean(), nullable=True))
    op.add_column("noc_services", sa.Column("last_error", sa.String(200), nullable=True))
    op.add_column("noc_services", sa.Column("last_ms", sa.Integer(), nullable=True))
    op.add_column("noc_services", sa.Column("last_rcode", sa.String(20), nullable=True))
    op.add_column(
        "noc_services",
        sa.Column("consecutive_failures", sa.Integer(),
                  nullable=False, server_default="0"),
    )
    op.add_column("noc_services", sa.Column("last_state_change_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_noc_services_last_ok",
        "noc_services",
        ["last_ok"],
        postgresql_where=sa.text("enabled = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_noc_services_last_ok", table_name="noc_services")
    op.drop_column("noc_services", "last_state_change_at")
    op.drop_column("noc_services", "consecutive_failures")
    op.drop_column("noc_services", "last_rcode")
    op.drop_column("noc_services", "last_ms")
    op.drop_column("noc_services", "last_error")
    op.drop_column("noc_services", "last_ok")
    op.drop_column("noc_services", "last_checked_at")
