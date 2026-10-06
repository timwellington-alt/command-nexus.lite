"""Persist VLAN audit results so subsequent runs can diff against history.

Revision ID: a114_vlan_audit_snapshots
Revises: a113_requester_role_scope
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a114_vlan_audit_snapshots"
down_revision = "a113_requester_role_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vlan_audit_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ran_by", sa.String(255), nullable=False),
        sa.Column("switch_count", sa.Integer, nullable=False, default=0),
        sa.Column("vlan_count", sa.Integer, nullable=False, default=0),
        sa.Column("failure_count", sa.Integer, nullable=False, default=0),
        sa.Column("data", sa.dialects.postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_vlan_audit_snapshots_ran_at", "vlan_audit_snapshots", ["ran_at"])


def downgrade() -> None:
    op.drop_index("ix_vlan_audit_snapshots_ran_at", table_name="vlan_audit_snapshots")
    op.drop_table("vlan_audit_snapshots")
