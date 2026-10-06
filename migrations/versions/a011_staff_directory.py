"""Staff directory cache table — synced from Google Workspace.

Revision ID: a011_staff_directory
Revises: a010_network_control
Create Date: 2026-03-28
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a011_staff_directory"
down_revision: Union[str, None] = "a010_network_control"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_directory",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("department", sa.String(100), nullable=True),
        sa.Column("org_unit", sa.String(500), nullable=True),
        sa.Column("building", sa.String(50), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_login", sa.String(50), nullable=True),
        sa.Column("google_id", sa.String(100), nullable=True),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_staff_directory_email", "staff_directory", ["email"])
    op.create_index("ix_staff_directory_building", "staff_directory", ["building"])


def downgrade() -> None:
    op.drop_table("staff_directory")
