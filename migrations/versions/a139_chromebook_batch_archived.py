"""Add archived_at column to chromebook_batches for auto-archive after 30d.

Saved batches are convenience artifacts for label reprints / manifest
regeneration. Batches older than 30 days rarely need reprinting; a
daily job (archive_stale_chromebook_batches) sets archived_at, and
list_batches hides them by default.

Revision ID: a139_chromebook_batch_archived
Revises: a138_halo_readings_desc_idx
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a139_chromebook_batch_archived"
down_revision = "a138_halo_readings_desc_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chromebook_batches",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chromebook_batches_archived_at "
        "ON chromebook_batches (archived_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chromebook_batches_archived_at")
    op.drop_column("chromebook_batches", "archived_at")
