"""Chromebook part model-compatibility tags on inventory_items.

`compatible_chromebook_models` — JSONB array of model tags (`G5`,
`G6`, `G7`, etc.). Only meaningful when the item's category is one of
the seeded Chromebook Part categories; other item_types ignore it.
Nullable — legacy rows and non-parts items just have NULL.

Chosen over a separate join table because:
  • Tags are a fixed small set (3 today) — no join needed
  • Per-item tagging is the natural granularity: a specific batch of
    LCDBros screens fits G5+G6 but a different SKU fits only G7
  • Backfill later is straightforward: UPDATE ... SET
    compatible_chromebook_models = '["G5"]' WHERE description LIKE
    '%G5%' AND category_id IN (...)

Index is a partial GIN on the JSONB — future 'find G6 screens across
buildings' queries become fast; NULL rows don't bloat the index.

Revision ID: a146_inventory_compat_models
Revises: a145_chromebook_repair_parts
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "a146_inventory_compat_models"
down_revision = "a145_chromebook_repair_parts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_items",
        sa.Column("compatible_chromebook_models", JSONB, nullable=True),
    )
    op.create_index(
        "ix_inventory_items_compat_models_gin",
        "inventory_items",
        ["compatible_chromebook_models"],
        postgresql_using="gin",
        postgresql_where=sa.text("compatible_chromebook_models IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_items_compat_models_gin", "inventory_items")
    op.drop_column("inventory_items", "compatible_chromebook_models")
