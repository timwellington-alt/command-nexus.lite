"""Inventory items — floor-plan position columns.

Mirror of the camera placement model: lets any inventory item (most
useful for fixed-equipment items, but works for assets too) be pinned
to a building floor plan with x/y SVG coordinates and survive re-ingest
when manually positioned.

Backfilled by the facility ingest service when a manifest carries
x/y per item — most of the EPE 37 facility items already have
coordinates from Web Claude's CAD extraction.

Revision ID: a078_item_floor
Revises: a077_cat_groups
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a078_item_floor"
down_revision: Union[str, None] = "a077_cat_groups"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inventory_items", sa.Column("floor_plan_x", sa.Float()))
    op.add_column("inventory_items", sa.Column("floor_plan_y", sa.Float()))
    op.add_column("inventory_items", sa.Column("floor_plan_floor", sa.Integer()))
    op.add_column("inventory_items", sa.Column("floor_plan_building_code", sa.String(20)))
    op.add_column("inventory_items",
        sa.Column("floor_plan_manually_positioned", sa.Boolean(),
                  nullable=False, server_default=sa.text("false")))
    op.create_index(
        "ix_inventory_items_floor_plan", "inventory_items",
        ["floor_plan_building_code", "floor_plan_floor"],
        postgresql_where=sa.text("floor_plan_x IS NOT NULL AND floor_plan_y IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_items_floor_plan", table_name="inventory_items")
    op.drop_column("inventory_items", "floor_plan_manually_positioned")
    op.drop_column("inventory_items", "floor_plan_building_code")
    op.drop_column("inventory_items", "floor_plan_floor")
    op.drop_column("inventory_items", "floor_plan_y")
    op.drop_column("inventory_items", "floor_plan_x")
