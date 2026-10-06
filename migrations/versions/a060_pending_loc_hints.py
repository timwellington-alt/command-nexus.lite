"""inventory_pending.location_hints — pre-filled location from email
subject + in-photo QR.

Single JSONB blob (vs five separate columns) because the data is purely
a UI hint — the canonical location lives on inventory_items after
confirmation. Keys: building_code, room, storage_rack, storage_shelf,
container_id.

Revision ID: a060_pending_loc_hints
Revises: a059_pending_assignee
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a060_pending_loc_hints"
down_revision: Union[str, None] = "a059_pending_assignee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_pending",
        sa.Column("location_hints", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inventory_pending", "location_hints")
