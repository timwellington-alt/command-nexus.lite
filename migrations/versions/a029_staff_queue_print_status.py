"""Add badge print status columns to staff_queue.

Revision ID: a029_print_status
Revises: a028_queue
Create Date: 2026-04-08
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a029_print_status"
down_revision: Union[str, None] = "a028_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pending | printed | error | dismissed
    op.add_column(
        "staff_queue",
        sa.Column("badge_print_status", sa.String(20), server_default="pending"),
    )
    op.add_column("staff_queue", sa.Column("badge_print_error", sa.Text()))
    op.add_column("staff_queue", sa.Column("badge_printed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("staff_queue", "badge_printed_at")
    op.drop_column("staff_queue", "badge_print_error")
    op.drop_column("staff_queue", "badge_print_status")
