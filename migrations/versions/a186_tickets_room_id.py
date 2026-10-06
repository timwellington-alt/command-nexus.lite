"""Add facility_room_id FK on tickets — Phase 4a of the room-centric
build. Backfills from (building, room) match against facility_rooms.
Nullable; not every ticket has a room (e.g. district-wide password
resets), and legacy tickets may reference retired room codes.

Revision ID: a186_tickets_room_id
Revises: a185_room_roster_facility_fk
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a186_tickets_room_id"
down_revision = "a185_room_roster_facility_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("facility_room_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_tickets_facility_room",
        "tickets", "facility_rooms",
        ["facility_room_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tickets_facility_room_id",
        "tickets", ["facility_room_id"],
        unique=False,
    )

    # Backfill existing tickets by (building, room) match.
    op.execute("""
        UPDATE tickets t
           SET facility_room_id = f.id
          FROM facility_rooms f
         WHERE f.building_code = t.building
           AND f.room_code     = t.room
           AND t.building IS NOT NULL
           AND t.room     IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_tickets_facility_room_id", "tickets")
    op.drop_constraint("fk_tickets_facility_room", "tickets", type_="foreignkey")
    op.drop_column("tickets", "facility_room_id")
