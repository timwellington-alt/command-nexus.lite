"""Per-room name overrides — editable text + independent label position.

Room functions evolve over the life of a building (REC → STORAGE →
WORKROOM); the SVG-baked name from CAD goes stale fast. `display_name`
overrides the baked text. `name_x`/`name_y` let the name label sit
somewhere other than directly below the room number (useful for
irregular rooms or where the number ended up cramped).

Same `manually_positioned`-style guard as the number: re-ingest of the
rooms JSON skips name overrides when the manual flag is set.

Revision ID: a073_room_name_overrides
Revises: a072_fp_lock
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a073_room_name_overrides"
down_revision: Union[str, None] = "a072_fp_lock"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("facility_rooms", sa.Column("display_name", sa.String(100)))
    op.add_column("facility_rooms", sa.Column("name_x", sa.Float()))
    op.add_column("facility_rooms", sa.Column("name_y", sa.Float()))
    op.add_column(
        "facility_rooms",
        sa.Column("name_manually_positioned", sa.Boolean(),
                  nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("facility_rooms", "name_manually_positioned")
    op.drop_column("facility_rooms", "name_y")
    op.drop_column("facility_rooms", "name_x")
    op.drop_column("facility_rooms", "display_name")
