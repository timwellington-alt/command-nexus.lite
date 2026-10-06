"""Staff ignore list — exclude staff from reconciliation.

Revision ID: a013_staff_ignore
Revises: a012_provisioning_profiles
Create Date: 2026-03-29
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a013_staff_ignore"
down_revision: Union[str, None] = "a012_provisioning_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_ignores",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ignored_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_staff_ignores_username"),
    )
    op.create_index("ix_staff_ignores_username", "staff_ignores", ["username"])


def downgrade() -> None:
    op.drop_table("staff_ignores")
