"""Add live-progress column to student_scan_runs.

The scanner walks 1,600+ student accounts; without a progress message
the UI can't tell a slow-but-working run from a stalled one.

Revision ID: a096_student_scan_progress
Revises: a095_blocklist_catalogues
Create Date: 2026-05-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a096_student_scan_progress"
down_revision: Union[str, None] = "a095_blocklist_catalogues"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "student_scan_runs",
        sa.Column("progress_message", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("student_scan_runs", "progress_message")
