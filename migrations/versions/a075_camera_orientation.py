"""Camera orientation on floor plan — rotation + FOV angle.

Lets users rotate the camera marker to reflect installed direction,
and adjust the field-of-view angle so the rendered cone reflects the
actual lens (60°/90°/120°/180° etc.). Defaults give a recognisable
generic 90° forward-facing cone if not set.

Revision ID: a075_cam_orient
Revises: a074_cam_pos
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a075_cam_orient"
down_revision: Union[str, None] = "a074_cam_pos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cameras", sa.Column("floor_plan_rotation_deg", sa.Integer(),
                                        nullable=False, server_default="0"))
    op.add_column("cameras", sa.Column("floor_plan_fov_deg", sa.Integer(),
                                        nullable=False, server_default="90"))


def downgrade() -> None:
    op.drop_column("cameras", "floor_plan_fov_deg")
    op.drop_column("cameras", "floor_plan_rotation_deg")
