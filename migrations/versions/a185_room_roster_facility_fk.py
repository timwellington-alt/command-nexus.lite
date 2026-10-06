"""Add facility_room_id FK on room_roster_cache — Phase 2 of the room
data consolidation. Backfills from (building, room) match against
facility_rooms. Nullable for now; Phase 4 flips to NOT NULL after two
weeks of clean state.

Revision ID: a185_room_roster_facility_fk
Revises: a184_roster_swis_fields
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a185_room_roster_facility_fk"
down_revision = "a184_roster_swis_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "room_roster_cache",
        sa.Column("facility_room_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_room_roster_facility_room",
        "room_roster_cache", "facility_rooms",
        ["facility_room_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_room_roster_facility_room_id",
        "room_roster_cache", ["facility_room_id"],
        unique=False,
    )

    # Backfill — one-shot match by (building, room) → facility_rooms(id).
    # Phase 1 confirmed 100% match; this should populate every row.
    op.execute("""
        UPDATE room_roster_cache r
           SET facility_room_id = f.id
          FROM facility_rooms f
         WHERE f.building_code = r.building
           AND f.room_code     = r.room
    """)


def downgrade() -> None:
    op.drop_index("ix_room_roster_facility_room_id", "room_roster_cache")
    op.drop_constraint("fk_room_roster_facility_room", "room_roster_cache", type_="foreignkey")
    op.drop_column("room_roster_cache", "facility_room_id")
