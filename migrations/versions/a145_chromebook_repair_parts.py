"""Chromebook repair parts — usage-tracking table + seed 8 part categories.

Adds:
  • Seed 8 InventoryCategory rows under group_name='Chromebook Parts'
    (Screen, Keyboard, Battery, Hinge, Palmrest, Bezel, Charger, Other).
    These tag existing/future inventory_items so the /chromebook/parts
    page and the ticket-side add-part UI can filter to just Chromebook
    consumables.
  • chromebook_repair_parts_used — one row per part decrement against a
    repair ticket. Frozen unit_cost_cents_at_use so historical avg-cost-
    per-repair queries stay accurate even if new stock arrives at
    different prices later. Reversible: undone_at + undone_by_email
    marks a soft-undo; the repository puts the stock back and refuses
    to count undone rows in aggregations.

Revision ID: a145_chromebook_repair_parts
Revises: a144_pending_order_tracking
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a145_chromebook_repair_parts"
down_revision = "a144_pending_order_tracking"
branch_labels = None
depends_on = None


_SEED_CATEGORIES = [
    ("Chromebook Screen",   "Chromebook Parts", "monitor"),
    ("Chromebook Keyboard", "Chromebook Parts", "keyboard"),
    ("Chromebook Battery",  "Chromebook Parts", "battery"),
    ("Chromebook Hinge",    "Chromebook Parts", "tool"),
    ("Chromebook Palmrest", "Chromebook Parts", "tool"),
    ("Chromebook Bezel",    "Chromebook Parts", "tool"),
    ("Chromebook Charger",  "Chromebook Parts", "plug"),
    ("Chromebook Other",    "Chromebook Parts", "package"),
]


def upgrade() -> None:
    op.create_table(
        "chromebook_repair_parts_used",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.Integer,
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("category_id", sa.Integer,
                  sa.ForeignKey("inventory_categories.id", ondelete="RESTRICT"),
                  nullable=False, index=True),
        sa.Column("inventory_item_id", sa.Integer,
                  sa.ForeignKey("inventory_items.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("building_code", sa.String(20), nullable=True, index=True),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("unit_cost_cents_at_use", sa.Integer, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("used_by_email", sa.String(255), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("undone_by_email", sa.String(255), nullable=True),
        sa.CheckConstraint("quantity > 0", name="ck_parts_used_qty_pos"),
    )
    op.create_index(
        "ix_parts_used_ticket_active",
        "chromebook_repair_parts_used",
        ["ticket_id"],
        postgresql_where=sa.text("undone_at IS NULL"),
    )

    # Seed part-type categories. `INSERT ... ON CONFLICT DO NOTHING`
    # so re-running the migration on a partially-seeded DB is safe.
    for name, group, icon in _SEED_CATEGORIES:
        op.execute(sa.text("""
            INSERT INTO inventory_categories
                (name, group_name, icon, default_is_loanable,
                 default_is_capitalized, sort_order, archived, created_at)
            VALUES (:name, :group, :icon, false, false, 100, false, NOW())
            ON CONFLICT (name) DO NOTHING
        """).bindparams(name=name, group=group, icon=icon))


def downgrade() -> None:
    op.drop_index("ix_parts_used_ticket_active", "chromebook_repair_parts_used")
    op.drop_table("chromebook_repair_parts_used")
    for name, _g, _i in _SEED_CATEGORIES:
        op.execute(sa.text("DELETE FROM inventory_categories WHERE name = :n")
                   .bindparams(n=name))
