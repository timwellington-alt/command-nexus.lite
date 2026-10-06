"""Per-user (and shared default) floor-plan view profiles.

Each profile captures the user's chosen layer/section state on a
building floor plan: arch sub-layers, trade overlays + sub-nodes, pin
kind visibility, sidebar section collapse state, optional zoom/pan.
Users save and switch between their own named profiles. A profile with
NULL user_id and is_default=TRUE is the shared cross-user default for
that (building, floor).

Revision ID: a080_view_profiles
Revises: a079_fp_pins
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a080_view_profiles"
down_revision: Union[str, None] = "a079_fp_pins"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "floor_plan_view_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("floor", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("state", sa.dialects.postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    # One name per (user, building, floor) — COALESCE makes shared defaults (user_id NULL) coexist
    op.execute("""
        CREATE UNIQUE INDEX uq_fp_view_profile_name
        ON floor_plan_view_profiles (COALESCE(user_id, 0), building_code, floor, name)
    """)
    # At most one shared default per (building, floor)
    op.execute("""
        CREATE UNIQUE INDEX uq_fp_view_profile_default
        ON floor_plan_view_profiles (building_code, floor)
        WHERE is_default AND user_id IS NULL
    """)
    op.create_index(
        "ix_fp_view_profile_user", "floor_plan_view_profiles",
        ["user_id", "building_code", "floor"],
    )


def downgrade() -> None:
    op.drop_index("ix_fp_view_profile_user", table_name="floor_plan_view_profiles")
    op.execute("DROP INDEX IF EXISTS uq_fp_view_profile_default")
    op.execute("DROP INDEX IF EXISTS uq_fp_view_profile_name")
    op.drop_table("floor_plan_view_profiles")
