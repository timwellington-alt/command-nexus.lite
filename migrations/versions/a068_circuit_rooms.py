"""Many-to-many between panel circuits and the rooms they serve.

One circuit can feed multiple rooms (corridor lighting, multi-classroom
receptacle runs). One room can be on multiple circuits (separate lights
and receptacles, redundant feeds). The free-text `area_served` column on
`inventory_panel_circuits` stays as the human-readable label; structured
room references live here so SVG-click-to-circuits and reverse-trace
queries work without text matching.

When the SVG building registry lands, the (building_code, room_code)
pair on this table will be the FK target into a canonical rooms table.
For now: free-text validated only at the application layer.

Revision ID: a068_circuit_rooms
Revises: a067_panel_circuits
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a068_circuit_rooms"
down_revision: Union[str, None] = "a067_panel_circuits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_panel_circuit_rooms",
        sa.Column(
            "circuit_id", sa.Integer(),
            sa.ForeignKey("inventory_panel_circuits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("room_code", sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint("circuit_id", "building_code", "room_code"),
    )
    # Reverse-direction index: "what circuits feed this room?"
    op.create_index(
        "ix_circuit_rooms_lookup",
        "inventory_panel_circuit_rooms",
        ["building_code", "room_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_circuit_rooms_lookup", table_name="inventory_panel_circuit_rooms")
    op.drop_table("inventory_panel_circuit_rooms")
