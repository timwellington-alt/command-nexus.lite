"""inventory_pending_orders: shipping addressee + notification timestamp.

- ship_to_name: raw name extracted from the order email / PDF (e.g.
  "Chance Dearth").
- ship_to_email: resolved email from the staff directory (fuzzy match).
  Falls back to the forwarder if no staff row matches.
- ship_to_resolved_via: bookkeeping so the UI / audit can see how we
  picked the email — one of 'staff_match', 'forwarder_fallback',
  'unresolved'.
- notification_sent_at: dedupes the "expect this package" email if the
  row ever gets reopened or reparsed.

Revision ID: a058_pending_ship_to
Revises: a057_pending_msrp
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a058_pending_ship_to"
down_revision: Union[str, None] = "a057_pending_msrp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inventory_pending_orders", sa.Column("ship_to_name", sa.String(length=255), nullable=True))
    op.add_column("inventory_pending_orders", sa.Column("ship_to_email", sa.String(length=255), nullable=True))
    op.add_column("inventory_pending_orders", sa.Column("ship_to_resolved_via", sa.String(length=30), nullable=True))
    op.add_column("inventory_pending_orders", sa.Column("notification_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("inventory_pending_orders", "notification_sent_at")
    op.drop_column("inventory_pending_orders", "ship_to_resolved_via")
    op.drop_column("inventory_pending_orders", "ship_to_email")
    op.drop_column("inventory_pending_orders", "ship_to_name")
