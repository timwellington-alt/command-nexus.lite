"""Floor plan obstacles (trees, adjacent buildings).

Free-form geometry that participates in camera FOV ray-casting
without being tied to any inventory item. Two shapes for now:
circles (trees, planters) and rotated boxes (adjacent buildings,
dumpster corrals).

Revision ID: a089_floor_plan_obstacles
Revises: a088_pin_kind_cart
Create Date: 2026-05-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a089_floor_plan_obstacles"
down_revision: Union[str, None] = "a088_pin_kind_cart"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "floor_plan_obstacles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("floor", sa.Integer, nullable=False, server_default="1"),
        sa.Column("shape", sa.String(10), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="generic"),
        sa.Column("x", sa.Float, nullable=False),
        sa.Column("y", sa.Float, nullable=False),
        sa.Column("radius", sa.Float, nullable=True),
        sa.Column("width", sa.Float, nullable=True),
        sa.Column("height", sa.Float, nullable=True),
        sa.Column("rotation_deg", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("shape IN ('circle','box')", name="ck_floor_plan_obstacles_shape"),
    )
    op.create_index(
        "ix_floor_plan_obstacles_building",
        "floor_plan_obstacles",
        ["building_code", "floor"],
    )


def downgrade() -> None:
    op.drop_index("ix_floor_plan_obstacles_building", table_name="floor_plan_obstacles")
    op.drop_table("floor_plan_obstacles")
