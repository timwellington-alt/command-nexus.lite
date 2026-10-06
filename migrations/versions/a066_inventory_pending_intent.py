"""Inventory pending — intent column for fixed-equipment intake.

`intent='fixed'` means the photo was emailed with `subject:inventory fixed`
and should be promoted into `inventory_items` with `item_type='fixed'`
when the reviewer confirms it.

Revision ID: a066_inventory_pending_intent
Revises: a065_facility_equipment
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a066_inventory_pending_intent"
down_revision: Union[str, None] = "a065_facility_equipment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_pending",
        sa.Column("intent", sa.String(20), nullable=False, server_default="asset"),
    )
    op.create_check_constraint(
        "ck_inventory_pending_intent",
        "inventory_pending",
        "intent IN ('asset', 'fixed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_inventory_pending_intent", "inventory_pending", type_="check")
    op.drop_column("inventory_pending", "intent")
