"""Persist rogue AP scan candidates so the operator can triage them across runs.

Revision ID: a115_rogue_ap_findings
Revises: a114_vlan_audit_snapshots
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a115_rogue_ap_findings"
down_revision = "a114_vlan_audit_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rogue_ap_findings",
        sa.Column("mac", sa.String(17), primary_key=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("hostname", sa.String(255), nullable=True),
        sa.Column("manufacturer", sa.String(100), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_rogue_ap_findings_status", "rogue_ap_findings", ["status", "last_seen"])


def downgrade() -> None:
    op.drop_index("ix_rogue_ap_findings_status", table_name="rogue_ap_findings")
    op.drop_table("rogue_ap_findings")
