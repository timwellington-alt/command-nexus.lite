"""Also link damage items to Chrome OS devices (Chromeflex desktops,
Chromebooks, etc.) via ``chromebook_cache_id`` FK.

Orthogonal to the inventory link — a damage item can point to at most
one of ``inventory_item_id`` OR ``chromebook_cache_id``, but neither
is required. The two sources cover different assets:

- ``inventory_items``: fixed assets, capitalized IT gear, tools.
- ``chromebook_cache``: every enrolled Chrome OS device including
  Chromebase / Chromeflex desktops.

ON DELETE SET NULL because the chromebook cache is periodically
pruned to reflect Google Admin state; a damage record should survive
the underlying cache row disappearing.

Revision ID: a170_damage_item_chromebook_link
Revises: a169_damage_item_inventory_link
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a170_damage_item_chromebook_link"
down_revision = "a169_damage_item_inventory_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "damage_items",
        sa.Column("chromebook_cache_id", sa.Integer, nullable=True),
    )
    op.create_foreign_key(
        "fk_damage_items_chromebook_cache",
        "damage_items", "chromebook_cache",
        ["chromebook_cache_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_damage_items_chromebook_cache",
        "damage_items", ["chromebook_cache_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_damage_items_chromebook_cache", table_name="damage_items")
    op.drop_constraint("fk_damage_items_chromebook_cache", "damage_items",
                       type_="foreignkey")
    op.drop_column("damage_items", "chromebook_cache_id")
