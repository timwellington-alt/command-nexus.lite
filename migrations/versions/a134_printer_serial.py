"""Add serial column to network_device_cache so the printer migration
tracker can disambiguate physical printers when sysname is repeated
(multiple iR-ADV 6855s, multiple c5840s, etc).

Source: LibreNMS devices.serial via SNMP — Canon iR-ADV reports an
8-char alphanumeric serial.

Revision ID: a134_printer_serial
Revises: a133_printer_share_name
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a134_printer_serial"
down_revision = "a133_printer_share_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("network_device_cache", sa.Column("serial", sa.String(64)))


def downgrade() -> None:
    op.drop_column("network_device_cache", "serial")
