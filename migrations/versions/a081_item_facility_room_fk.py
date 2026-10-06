"""Inventory items — link to facility_rooms by FK + backfill matches.

Replaces free-text `inventory_items.room` lookups with a proper FK to
the canonical room registry. Old rows keep their text in the existing
column; new/edited rows must reference a real room. Mismatches surface
as facility_room_id NULL so admins can clean them up.

Revision ID: a081_item_room_fk
Revises: a080_view_profiles
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a081_item_room_fk"
down_revision: Union[str, None] = "a080_view_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_items",
        sa.Column("facility_room_id", sa.Integer(),
                  sa.ForeignKey("facility_rooms.id", ondelete="SET NULL")),
    )
    op.create_index(
        "ix_inventory_items_facility_room", "inventory_items",
        ["facility_room_id"],
        postgresql_where=sa.text("facility_room_id IS NOT NULL"),
    )
    # Backfill where (building_code, room) matches a facility_rooms entry.
    op.execute("""
        UPDATE inventory_items i
           SET facility_room_id = r.id
          FROM facility_rooms r
         WHERE i.deleted_at IS NULL
           AND r.deleted_at IS NULL
           AND i.building_code = r.building_code
           AND i.room = r.room_code
    """)


def downgrade() -> None:
    op.drop_index("ix_inventory_items_facility_room", table_name="inventory_items")
    op.drop_column("inventory_items", "facility_room_id")
