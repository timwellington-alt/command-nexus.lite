"""Add Paxton, AD, and HR cache tables for sync tiering.

Revision ID: a022_sync_caches
Revises: a021_docs_module
Create Date: 2026-04-05
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a022_sync_caches"
down_revision: Union[str, None] = "a021_docs_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paxton_user_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("paxton_id", sa.Integer, unique=True, nullable=False, index=True),
        sa.Column("first_name", sa.String(100)),
        sa.Column("last_name", sa.String(100)),
        sa.Column("display_name", sa.String(255)),
        sa.Column("email", sa.String(255), index=True),
        sa.Column("department", sa.String(255)),
        sa.Column("department_id", sa.Integer),
        sa.Column("access_levels", sa.Text),
        sa.Column("has_image", sa.Boolean, default=False),
        sa.Column("enabled", sa.Boolean, default=True),
        sa.Column("pin", sa.String(50)),
        sa.Column("activate_date", sa.String(50)),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "ad_user_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(100), unique=True, nullable=False, index=True),
        sa.Column("email", sa.String(255), index=True),
        sa.Column("first_name", sa.String(100)),
        sa.Column("last_name", sa.String(100)),
        sa.Column("display_name", sa.String(255)),
        sa.Column("title", sa.String(255)),
        sa.Column("department", sa.String(255)),
        sa.Column("ou", sa.String(500)),
        sa.Column("enabled", sa.Boolean, default=True),
        sa.Column("groups", sa.Text),
        sa.Column("last_logon", sa.String(50)),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "hr_staff_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), index=True),
        sa.Column("name", sa.String(255), index=True),
        sa.Column("position", sa.String(255)),
        sa.Column("school", sa.String(100)),
        sa.Column("classification", sa.String(100)),
        sa.Column("tab_source", sa.String(100)),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("hr_staff_cache")
    op.drop_table("ad_user_cache")
    op.drop_table("paxton_user_cache")
