"""Inventory budgets — per-department, per-fiscal-year allocations.

Starts as one row (Tech Parts & Supplies, FY 2026-27) but the schema
supports adding more departments/buckets later without churn. Each
`inventory_pending_orders` row is optionally attributed to a budget
via the new `budget_id` FK — spend is aggregated as SUM(total_cents)
per budget when order_date falls inside the budget's fiscal window.

Revision ID: a153_inventory_budgets
Revises: a152_vendor_attachments
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a153_inventory_budgets"
down_revision = "a152_vendor_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inventory_budgets",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False),
        # Department identifier — kept as a bare string so a new
        # department (facilities, transportation, etc.) can be added
        # without a schema change. Enforced via the UI, not the DB.
        sa.Column("department", sa.String(40), nullable=False, index=True),
        sa.Column("fiscal_year_start", sa.Date, nullable=False),
        sa.Column("fiscal_year_end", sa.Date, nullable=False),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("department", "fiscal_year_start", "name",
                            name="uq_inventory_budgets_dept_fy_name"),
    )

    # FK from pending orders to budget. Nullable — unattributed orders
    # simply don't count against any budget line.
    op.add_column(
        "inventory_pending_orders",
        sa.Column("budget_id", sa.Integer,
                  sa.ForeignKey("inventory_budgets.id", ondelete="SET NULL"),
                  nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_column("inventory_pending_orders", "budget_id")
    op.drop_table("inventory_budgets")
