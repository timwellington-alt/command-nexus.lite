"""Many-to-many between panel circuits and equipment items they feed.

Complement to inventory_panel_circuit_rooms — that links circuits to
rooms; this links circuits to specific fixed-equipment items (CUH-1,
AHU-2, RTU-3, etc.). Most useful when the area_served text references
equipment by tag (e.g. 'CUH-1' on the panel directory means the
breaker feeds Cabinet Unit Heater #1, which we already have as a row
in inventory_items).

Revision ID: a076_circuit_items
Revises: a075_cam_orient
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a076_circuit_items"
down_revision: Union[str, None] = "a075_cam_orient"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_panel_circuit_items",
        sa.Column("circuit_id", sa.Integer(),
                  sa.ForeignKey("inventory_panel_circuits.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("item_id", sa.Integer(),
                  sa.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="parsed"),
        sa.PrimaryKeyConstraint("circuit_id", "item_id"),
        sa.CheckConstraint("source IN ('parsed', 'manual')", name="ck_circuit_items_source"),
    )
    op.create_index(
        "ix_circuit_items_item", "inventory_panel_circuit_items", ["item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_circuit_items_item", table_name="inventory_panel_circuit_items")
    op.drop_table("inventory_panel_circuit_items")
