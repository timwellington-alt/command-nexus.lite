"""Add chromebook_batches table and recovery_status column.

Revision ID: a023_chromebook_batches
Revises: a022_sync_caches
Create Date: 2026-04-05
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a023_chromebook_batches"
down_revision: Union[str, None] = "a022_sync_caches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chromebook_cache", sa.Column("recovery_status", sa.String(20)))

    op.create_table(
        "chromebook_batches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("building", sa.String(100)),
        sa.Column("room", sa.String(100)),
        sa.Column("device_ids", sa.Text, nullable=False),
        sa.Column("device_count", sa.Integer, default=0),
        sa.Column("asset_prefix", sa.String(50)),
        sa.Column("asset_start", sa.Integer),
        sa.Column("asset_digits", sa.Integer),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("chromebook_batches")
    op.drop_column("chromebook_cache", "recovery_status")
