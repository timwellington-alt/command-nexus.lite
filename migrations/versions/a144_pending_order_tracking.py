"""Add tracking # + carrier + notify timestamps to inventory pending orders.

Phase 1 of the receiving-module tracking work — no carrier API polling
yet, just a place to store the tracking # from the vendor confirmation
and a marker for when the addressee was notified.

Revision ID: a144_pending_order_tracking
Revises: a143_repair_troubleshooting_tips
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a144_pending_order_tracking"
down_revision = "a143_repair_troubleshooting_tips"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inventory_pending_orders",
                  sa.Column("tracking_number", sa.String(100), nullable=True))
    op.add_column("inventory_pending_orders",
                  sa.Column("tracking_carrier", sa.String(30), nullable=True))
    op.add_column("inventory_pending_orders",
                  sa.Column("tracking_updated_at",
                            sa.DateTime(timezone=True), nullable=True))
    op.add_column("inventory_pending_orders",
                  sa.Column("tracking_notified_at",
                            sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_pending_orders_tracking_number",
        "inventory_pending_orders",
        ["tracking_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_orders_tracking_number", "inventory_pending_orders")
    op.drop_column("inventory_pending_orders", "tracking_notified_at")
    op.drop_column("inventory_pending_orders", "tracking_updated_at")
    op.drop_column("inventory_pending_orders", "tracking_carrier")
    op.drop_column("inventory_pending_orders", "tracking_number")
