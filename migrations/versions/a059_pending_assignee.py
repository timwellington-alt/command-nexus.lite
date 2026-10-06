"""inventory_pending_orders.assigned_user_id — per-order receiver.

The parser's ship_to resolver suggests an addressee. An admin confirms
(or overrides) it on the receiving page; at that point we write
assigned_user_id. Visibility rules downstream key on this: admins see
all, non-admins see only orders where they're the assignee.

Revision ID: a059_pending_assignee
Revises: a058_pending_ship_to
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a059_pending_assignee"
down_revision: Union[str, None] = "a058_pending_ship_to"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_pending_orders",
        sa.Column("assigned_user_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_pending_orders_assigned_user",
        "inventory_pending_orders", "users",
        ["assigned_user_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_pending_orders_assigned_user",
        "inventory_pending_orders", ["assigned_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_orders_assigned_user", table_name="inventory_pending_orders")
    op.drop_constraint("fk_pending_orders_assigned_user", "inventory_pending_orders", type_="foreignkey")
    op.drop_column("inventory_pending_orders", "assigned_user_id")
