"""Network extras — cast_devices table.

Revision ID: a015_network_extras
Revises: a013_staff_ignore
Create Date: 2026-03-29
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a015_network_extras"
down_revision: Union[str, None] = "a013_staff_ignore"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cast_devices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("ip", sa.String(50), nullable=False),
        sa.Column("auto_cast", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("cast_devices")
