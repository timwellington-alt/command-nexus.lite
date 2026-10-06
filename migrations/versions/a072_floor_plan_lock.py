"""Per-(building, floor) lock state for floor-plan label editing.

Presence of a row = the floor plan's room labels are locked. Absence =
unlocked. Locking is a UX guard (so a stray drag doesn't move a label
that's been intentionally placed), not a security boundary.

Revision ID: a072_fp_lock
Revises: a071_room_manual_pos
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a072_fp_lock"
down_revision: Union[str, None] = "a071_room_manual_pos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "facility_floor_plan_locks",
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("floor", sa.String(20), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("locked_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("locked_by_email", sa.String(255)),
        sa.PrimaryKeyConstraint("building_code", "floor"),
    )


def downgrade() -> None:
    op.drop_table("facility_floor_plan_locks")
