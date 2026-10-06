"""Inventory shipping docs — add `doc_kind` and allow system-captured rows.

Adds `doc_kind` (default 'shipping') so we can persist the original PO
invoice PDF alongside user-uploaded packing slips. Makes
`captured_by_user_id` nullable since invoice rows are created by the
inbox watcher, not a user.

Revision ID: a062_inv_doc_kind
Revises: a061_carehawk_module
Create Date: 2026-04-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a062_inv_doc_kind"
down_revision: Union[str, None] = "a061_carehawk_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_shipping_docs",
        sa.Column("doc_kind", sa.String(20), nullable=False, server_default="shipping"),
    )
    op.create_check_constraint(
        "inventory_shipping_docs_doc_kind_chk",
        "inventory_shipping_docs",
        "doc_kind IN ('shipping','invoice')",
    )
    op.alter_column("inventory_shipping_docs", "captured_by_user_id", nullable=True)


def downgrade() -> None:
    op.alter_column("inventory_shipping_docs", "captured_by_user_id", nullable=False)
    op.drop_constraint("inventory_shipping_docs_doc_kind_chk", "inventory_shipping_docs", type_="check")
    op.drop_column("inventory_shipping_docs", "doc_kind")
