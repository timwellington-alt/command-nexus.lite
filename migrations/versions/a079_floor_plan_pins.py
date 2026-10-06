"""Generic floor-plan pin table for cross-module equipment.

Lets us place arbitrary records from other modules (wireless APs,
Paxton doors, phone extensions, future kinds) on a building floor
plan without adding columns to each module's primary table.

Cameras and inventory_items keep their per-table columns since
those flows are already shipped and they have placement-specific
extras (camera FOV/rotation, item room/manually_positioned).

Revision ID: a079_fp_pins
Revises: a078_item_floor
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a079_fp_pins"
down_revision: Union[str, None] = "a078_item_floor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "floor_plan_pins",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("target_kind", sa.String(20), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("floor", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("rotation_deg", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("target_kind", "target_id", name="uq_floor_plan_pins_target"),
        sa.CheckConstraint("target_kind IN ('ap', 'door', 'phone')",
                           name="ck_floor_plan_pins_kind"),
    )
    op.create_index(
        "ix_floor_plan_pins_building", "floor_plan_pins",
        ["building_code", "floor"],
    )


def downgrade() -> None:
    op.drop_index("ix_floor_plan_pins_building", table_name="floor_plan_pins")
    op.drop_table("floor_plan_pins")
