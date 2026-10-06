"""Enhance staff_queue with provisioning workflow fields.

Revision ID: a028_queue
Revises: a027_staff_reconciliation
Create Date: 2026-04-08
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a028_queue"
down_revision: Union[str, None] = "a027_staff_reconciliation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("staff_queue", sa.Column("photo_path", sa.String(500)))
    op.add_column("staff_queue", sa.Column("preferred_name", sa.String(100)))
    op.add_column("staff_queue", sa.Column("room", sa.String(20)))
    op.add_column("staff_queue", sa.Column("classification", sa.String(50)))
    op.add_column("staff_queue", sa.Column("position", sa.String(255)))
    op.add_column("staff_queue", sa.Column("school", sa.String(100)))
    op.add_column("staff_queue", sa.Column("source_detail", sa.Text()))
    op.add_column("staff_queue", sa.Column("systems", sa.Text(),
                  server_default='{"google":true,"ad":true,"paxton":true,"phone":false}'))
    op.add_column("staff_queue", sa.Column("password_hash", sa.String(255)))
    op.add_column("staff_queue", sa.Column("expected_email", sa.String(255)))
    op.add_column("staff_queue", sa.Column("extension", sa.String(20)))
    op.add_column("staff_queue", sa.Column("submitted_by", sa.String(255)))
    op.add_column("staff_queue", sa.Column("reviewed_by", sa.String(255)))


def downgrade() -> None:
    for col in [
        "photo_path", "preferred_name", "room", "classification",
        "position", "school", "source_detail", "systems",
        "password_hash", "expected_email", "extension",
        "submitted_by", "reviewed_by",
    ]:
        op.drop_column("staff_queue", col)
