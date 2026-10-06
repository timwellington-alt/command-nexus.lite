"""Add rack/shelf/container fields to inventory_items.

Building + room already locate an item in the physical space; these fields
refine that down to "which rack / which shelf / which box" inside the IT
storage closet. All nullable — pre-existing items stay un-placed until a
user sets them. Indexed so QR-driven shelf/container lookups are fast.

Revision ID: a052_inventory_storage_location
Revises: a051_inventory_serial_optional
Create Date: 2026-04-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a052_inventory_storage_location"
down_revision: Union[str, None] = "a051_inventory_serial_optional"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inventory_items", sa.Column("storage_rack", sa.String(10)))
    op.add_column("inventory_items", sa.Column("storage_shelf", sa.String(20)))
    op.add_column("inventory_items", sa.Column("container_id", sa.String(50)))
    op.create_index(
        "ix_inventory_items_storage",
        "inventory_items",
        ["storage_rack", "storage_shelf"],
    )
    op.create_index(
        "ix_inventory_items_container",
        "inventory_items",
        ["container_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_items_container", table_name="inventory_items")
    op.drop_index("ix_inventory_items_storage", table_name="inventory_items")
    op.drop_column("inventory_items", "container_id")
    op.drop_column("inventory_items", "storage_shelf")
    op.drop_column("inventory_items", "storage_rack")
