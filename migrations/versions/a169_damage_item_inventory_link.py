"""Link damage items to indexed inventory assets by FK.

A damage item can optionally reference an ``inventory_items`` row so
the exact physical asset (serial, asset tag, purchase info, location)
is a click away from the damage record. Nullable — many damage items
(cable plant, endpoints not in inventory, one-off building hardware)
have nothing to link to. ON DELETE SET NULL because deleting the
inventory item shouldn't purge the damage record.

Revision ID: a169_damage_item_inventory_link
Revises: a168_switch_poe_health
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a169_damage_item_inventory_link"
down_revision = "a168_switch_poe_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "damage_items",
        sa.Column("inventory_item_id", sa.Integer, nullable=True),
    )
    op.create_foreign_key(
        "fk_damage_items_inventory_item",
        "damage_items", "inventory_items",
        ["inventory_item_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_damage_items_inventory_item",
        "damage_items", ["inventory_item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_damage_items_inventory_item", table_name="damage_items")
    op.drop_constraint("fk_damage_items_inventory_item", "damage_items",
                       type_="foreignkey")
    op.drop_column("damage_items", "inventory_item_id")
