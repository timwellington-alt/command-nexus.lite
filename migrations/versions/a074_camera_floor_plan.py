"""Camera floor-plan positioning.

Lets users drag Wave cameras onto the building floor plan and persist
the (x, y, floor) so the marker stays put.

Revision ID: a074_cam_pos
Revises: a073_room_name_overrides
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a074_cam_pos"
down_revision: Union[str, None] = "a073_room_name_overrides"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("floor_plan_x", sa.Float()))
    op.add_column("cameras", sa.Column("floor_plan_y", sa.Float()))
    op.add_column("cameras", sa.Column("floor_plan_floor", sa.Integer()))
    op.add_column("cameras", sa.Column("floor_plan_building_code", sa.String(20)))


def downgrade() -> None:
    op.drop_column("cameras", "floor_plan_building_code")
    op.drop_column("cameras", "floor_plan_floor")
    op.drop_column("cameras", "floor_plan_y")
    op.drop_column("cameras", "floor_plan_x")
