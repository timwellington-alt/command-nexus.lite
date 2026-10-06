"""Add share_name to printer_migration_status so the tracker can show
print-server queue names rather than LibreNMS sysnames (which for older
HP/Dell models are uninformative like 'npi846308').

Revision ID: a133_printer_share_name
Revises: a132_printer_migration
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a133_printer_share_name"
down_revision = "a132_printer_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "printer_migration_status",
        sa.Column("share_name", sa.String(100)),
    )


def downgrade() -> None:
    op.drop_column("printer_migration_status", "share_name")
