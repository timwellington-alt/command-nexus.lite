"""Camera FOV reach distance for floor-plan cone visualization.

Existing fov_deg is the cone's *angular width*; this adds the *radial
reach* (in SVG coords) so the editor can shorten/lengthen the cone per
camera. Used by the wall-clipping renderer to bound ray-cast distance.

Revision ID: a085_camera_fov_range
Revises: a084_trade_offsets
Create Date: 2026-05-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a085_camera_fov_range"
down_revision: Union[str, None] = "a084_trade_offsets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cameras",
        sa.Column("floor_plan_fov_range", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cameras", "floor_plan_fov_range")
