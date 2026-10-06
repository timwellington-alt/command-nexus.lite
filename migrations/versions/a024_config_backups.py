"""Add config_backups table for switch config history.

Revision ID: a024_config_backups
Revises: a023_chromebook_batches
Create Date: 2026-04-06
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a024_config_backups"
down_revision: Union[str, None] = "a023_chromebook_batches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "config_backups",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.Integer, index=True),
        sa.Column("hostname", sa.String(255)),
        sa.Column("ip", sa.String(50)),
        sa.Column("filepath", sa.String(500)),
        sa.Column("config_hash", sa.String(64)),
        sa.Column("config_size", sa.Integer),
        sa.Column("success", sa.Boolean, default=False),
        sa.Column("error_message", sa.Text),
        sa.Column("backup_at", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True),
    )


def downgrade() -> None:
    op.drop_table("config_backups")
