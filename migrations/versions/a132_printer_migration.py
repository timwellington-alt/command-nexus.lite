"""Printer migration tracker — per-printer checkbox state for the
VLAN-94 / DHCP-reservation / print-server / Google-Admin cutover.

Temporary scaffolding while ~31 printers are migrated to the new print
VLAN. Each row tracks four boolean steps + a free-text notes field.
Drop the table when the migration is done.

Revision ID: a132_printer_migration
Revises: a131_ap_rename_perm
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a132_printer_migration"
down_revision = "a131_ap_rename_perm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "printer_migration_status",
        sa.Column("device_id", sa.Integer, primary_key=True),
        sa.Column("step_dhcp_switched", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("step_reservation_added", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("step_print_server", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("step_google_admin", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_by", sa.String(255)),
    )


def downgrade() -> None:
    op.drop_table("printer_migration_status")
