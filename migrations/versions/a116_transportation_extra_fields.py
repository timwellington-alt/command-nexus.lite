"""Extend ticket_transportation with FMX-parity fields.

Adds event_name, pickup_location, return_date, vehicle_count, chaperones,
overnight, handicap_bus, meal_stop. Required for the redesigned
transportation request form (2026-06-03).

Revision ID: a116_transportation_extra_fields
Revises: a115_rogue_ap_findings
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a116_transportation_extra_fields"
down_revision = "a115_rogue_ap_findings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket_transportation", sa.Column("event_name", sa.String(200), nullable=True))
    op.add_column("ticket_transportation", sa.Column("pickup_location", sa.String(255), nullable=True))
    op.add_column("ticket_transportation", sa.Column("return_date", sa.Date(), nullable=True))
    op.add_column("ticket_transportation", sa.Column("vehicle_count", sa.Integer(), nullable=True))
    op.add_column("ticket_transportation", sa.Column("chaperones", sa.dialects.postgresql.JSONB(), nullable=True))
    op.add_column("ticket_transportation", sa.Column("overnight", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("ticket_transportation", sa.Column("handicap_bus", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("ticket_transportation", sa.Column("meal_stop", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("ticket_transportation", "meal_stop")
    op.drop_column("ticket_transportation", "handicap_bus")
    op.drop_column("ticket_transportation", "overnight")
    op.drop_column("ticket_transportation", "chaperones")
    op.drop_column("ticket_transportation", "vehicle_count")
    op.drop_column("ticket_transportation", "return_date")
    op.drop_column("ticket_transportation", "pickup_location")
    op.drop_column("ticket_transportation", "event_name")
