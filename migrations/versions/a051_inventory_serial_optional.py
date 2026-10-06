"""Make inventory_items.serial_number nullable.

Stock items don't have real device serials — the receiving flow used to
invent `STK-LINE-{id}` placeholders to satisfy NOT NULL. With this change,
assets keep their required-at-the-schema-level serial (enforced by Pydantic),
stock items may be NULL.

Revision ID: a051_inventory_serial_optional
Revises: a050_inventory_module
Create Date: 2026-04-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a051_inventory_serial_optional"
down_revision: Union[str, None] = "a050_inventory_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "inventory_items", "serial_number",
        existing_type=sa.String(255),
        nullable=True,
    )


def downgrade() -> None:
    # Can't restore NOT NULL if any rows have NULL serial. Backfill with a
    # synthetic placeholder so the constraint can re-apply.
    op.execute(
        "UPDATE inventory_items SET serial_number = 'UNKNOWN-' || id "
        "WHERE serial_number IS NULL"
    )
    op.alter_column(
        "inventory_items", "serial_number",
        existing_type=sa.String(255),
        nullable=False,
    )
