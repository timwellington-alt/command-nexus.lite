"""Allow `created_via='facility_ingest'` on inventory_items.

Items created by the Web Claude facility-inventory ingest (CAD + spec
sheet extraction) need a distinct provenance so the re-ingest stale-
cleanup can target them without touching manually-created items.

Revision ID: a070_created_via_facility_ingest
Revises: a069_facility_rooms
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a070_created_via_facility_ingest"
down_revision: Union[str, None] = "a069_facility_rooms"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_inventory_items_created_via", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_created_via",
        "inventory_items",
        "created_via IN ('manual', 'scan_commission', 'order_receipt', 'bulk_import', "
        "'email_intake', 'email_intake_fixed', 'libre_import', 'facility_ingest')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_inventory_items_created_via", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_created_via",
        "inventory_items",
        "created_via IN ('manual', 'scan_commission', 'order_receipt', 'bulk_import', "
        "'email_intake', 'email_intake_fixed', 'libre_import')",
    )
