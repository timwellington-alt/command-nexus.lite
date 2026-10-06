"""inventory_pending.msrp_cents — Gemini-estimated MSRP at intake time.

Filled when the inbox watcher analyzes a photo so the reviewer sees a
ballpark price already populated. Reviewer can edit before confirming.

Revision ID: a057_pending_msrp
Revises: a056_inv_pending_msrp
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a057_pending_msrp"
down_revision: Union[str, None] = "a056_inv_pending_msrp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inventory_pending", sa.Column("msrp_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("inventory_pending", "msrp_cents")
