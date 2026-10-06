"""Track manually-set floor-plan label positions on facility_rooms.

When a user drags a room label on the floor-plan viewer, we update
`floor_plan_x` / `floor_plan_y` and flip `manually_positioned=true`.
The facility ingest skips x/y updates for rooms with the flag set,
so the user-positioned labels survive future re-ingests of the
extracted rooms JSON.

Reset action clears the flag — next ingest restores the
extraction-derived centroid.

Revision ID: a071_room_manual_pos
Revises: a070_created_via_facility_ingest
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a071_room_manual_pos"
down_revision: Union[str, None] = "a070_created_via_facility_ingest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facility_rooms",
        sa.Column("manually_positioned", sa.Boolean(),
                  nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("facility_rooms", "manually_positioned")
