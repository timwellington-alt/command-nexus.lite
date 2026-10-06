"""Add phone_extension_cache table.

Revision ID: a020_phone_cache
Revises: a019_chromebook_clever
Create Date: 2026-04-02
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a020_phone_cache"
down_revision: Union[str, None] = "a019_chromebook_clever"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "phone_extension_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("extension", sa.String(20), unique=True, nullable=False, index=True),
        sa.Column("caller_id_name", sa.String(255)),
        sa.Column("email", sa.String(255)),
        sa.Column("department", sa.String(255)),
        sa.Column("account_type", sa.String(50)),
        sa.Column("out_of_service", sa.String(10)),
        sa.Column("model", sa.String(100)),
        sa.Column("ip", sa.String(50)),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("phone_extension_cache")
