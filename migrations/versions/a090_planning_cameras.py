"""Floor plan planning cameras (proposed-coverage layer).

Shared layer of "what if we put a camera here" markers used by SROs
and admins together to plan camera additions before any inventory
exists. Same FOV ray-cast as real cameras, distinct color (amber).

Revision ID: a090_planning_cameras
Revises: a089_floor_plan_obstacles
Create Date: 2026-05-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a090_planning_cameras"
down_revision: Union[str, None] = "a089_floor_plan_obstacles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "floor_plan_planning_cameras",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("floor", sa.Integer, nullable=False, server_default="1"),
        sa.Column("name", sa.String(80), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("x", sa.Float, nullable=False),
        sa.Column("y", sa.Float, nullable=False),
        sa.Column("rotation_deg", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fov_deg", sa.Integer, nullable=False, server_default="90"),
        sa.Column("fov_range", sa.Float, nullable=False, server_default="80"),
        sa.Column("created_by_email", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_floor_plan_planning_cameras_building",
        "floor_plan_planning_cameras",
        ["building_code", "floor"],
    )


def downgrade() -> None:
    op.drop_index("ix_floor_plan_planning_cameras_building", table_name="floor_plan_planning_cameras")
    op.drop_table("floor_plan_planning_cameras")
