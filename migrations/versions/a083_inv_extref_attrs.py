"""Inventory items: dedicated external_ref + attrs columns.

Replaces the FACILITY:{ref} stuffed-into-po_number hack with a real
external_ref column. Adds an attrs JSONB column so facility-equipment
metadata (panel voltage, main_amps, capacity_kw, etc.) lives in
structured form instead of being flattened into the notes text blob.

Backfill:
  - external_ref ← po_number stripped of 'FACILITY:' prefix, where present
  - attrs ← {} (empty JSONB) for everything; service layer populates
    fields on next ingest

Revision ID: a083_inv_extref_attrs
Revises: a082_chat_module
Create Date: 2026-05-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a083_inv_extref_attrs"
down_revision: Union[str, None] = "a082_chat_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inventory_items", sa.Column("external_ref", sa.String(100), nullable=True))
    op.add_column("inventory_items", sa.Column("attrs", postgresql.JSONB(), nullable=True))

    # Partial unique-by-(building,external_ref) so each facility-ingested
    # item has a stable identifier per building. Partial because asset
    # items + non-facility items may have NULL external_ref.
    op.create_index(
        "ix_inv_items_building_extref",
        "inventory_items",
        ["building_code", "external_ref"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL AND deleted_at IS NULL"),
    )

    # Backfill from existing po_number FACILITY: prefix
    op.execute("""
        UPDATE inventory_items
        SET external_ref = SUBSTRING(po_number FROM 10)
        WHERE created_via = 'facility_ingest'
          AND po_number LIKE 'FACILITY:%'
          AND external_ref IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_inv_items_building_extref", table_name="inventory_items")
    op.drop_column("inventory_items", "attrs")
    op.drop_column("inventory_items", "external_ref")
