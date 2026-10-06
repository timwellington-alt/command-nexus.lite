"""Canonical room registry — facility_rooms.

Single source of truth for "what rooms exist, where are they on the
floor plan, what are they used for." Both inventory and (later) tickets
read from this table; neither owns the data.

Seeded from two sources on apply:
1. `room_roster_cache` (building, room) pairs — populates teacher-bearing
   rooms across PHS/PES/EPE.
2. `inventory_items` (building_code, room) — picks up rooms that have
   inventory but never appeared on a roster (storage closets, server
   rooms, etc.).

Both seeds are deduped via the (building, room) UNIQUE constraint and
inserted with `function=NULL`. First admin visit to the rooms page
shows everything as unassigned; functions get filled in via the UI.

Floor-plan coordinates (`floor_plan_x` / `floor_plan_y`) are NULL until
the per-building floor-plan ingest job populates them from the rooms
JSON. Floor number same — populated when the JSON ingest runs.

Revision ID: a069_facility_rooms
Revises: a068_circuit_rooms
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a069_facility_rooms"
down_revision: Union[str, None] = "a068_circuit_rooms"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "facility_rooms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("floor", sa.Integer()),
        sa.Column("room_code", sa.String(50), nullable=False),
        sa.Column("function", sa.String(40)),
        sa.Column("floor_plan_x", sa.Float()),
        sa.Column("floor_plan_y", sa.Float()),
        sa.Column("notes", sa.Text()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("building_code", "room_code",
                            name="uq_facility_rooms_bld_room"),
    )
    op.create_index(
        "ix_facility_rooms_building_floor", "facility_rooms",
        ["building_code", "floor"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ── Seed from room_roster_cache + inventory_items (idempotent) ────
    op.execute("""
        INSERT INTO facility_rooms (building_code, room_code)
        SELECT DISTINCT building, room
        FROM (
            SELECT building, room
            FROM room_roster_cache
            WHERE building IS NOT NULL AND room IS NOT NULL
              AND TRIM(building) <> '' AND TRIM(room) <> ''
            UNION
            SELECT building_code AS building, room
            FROM inventory_items
            WHERE deleted_at IS NULL
              AND building_code IS NOT NULL AND room IS NOT NULL
              AND TRIM(building_code) <> '' AND TRIM(room) <> ''
        ) s
        ON CONFLICT (building_code, room_code) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_index("ix_facility_rooms_building_floor", table_name="facility_rooms")
    op.drop_table("facility_rooms")
