"""Map inventory categories to budget lines.

A line's budget is now driven by its category. Each category is linked
to at most one budget via inventory_categories.budget_id (nullable).
Order-level inventory_pending_orders.budget_id stays as a fallback
for uncategorized lines so unattributed spend doesn't silently vanish.

Revision ID: a154_category_budget_mapping
Revises: a153_inventory_budgets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a154_category_budget_mapping"
down_revision = "a153_inventory_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_categories",
        sa.Column("budget_id", sa.Integer,
                  sa.ForeignKey("inventory_budgets.id", ondelete="SET NULL"),
                  nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_column("inventory_categories", "budget_id")
