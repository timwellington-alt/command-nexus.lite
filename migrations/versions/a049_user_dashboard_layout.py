"""Add users.dashboard_layout JSON column for per-user card selection/order.

NULL = use the default layout (all cards the user's permissions allow,
in registry order).

Revision ID: a049_user_dashboard_layout
Revises: a048_index_user_voice_recipient
Create Date: 2026-04-16
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a049_user_dashboard_layout"
down_revision: Union[str, None] = "a048_index_user_voice_recipient"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("dashboard_layout", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "dashboard_layout")
