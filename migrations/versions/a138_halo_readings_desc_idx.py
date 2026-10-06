"""Add descending-recorded_at index on halo_readings for fleet-overview perf.

The /api/security/halo/fleet-overview endpoint builds a `latest` CTE via
DISTINCT ON to pull each device's most-recent value per metric. The
original (device_id, metric, recorded_at ASC) index forces an Incremental
Sort to produce DESC order, costing ~210 ms per device or ~4-5 s for the
21-device fleet — Cloudflare 524s on a 14M-row table.

The new index orders recorded_at DESC so DISTINCT ON picks the latest
row directly without sorting. Combined with a 30-minute recency filter
in the LATERAL subquery, the endpoint now runs in ~1 s.

Built CONCURRENTLY so the index creation doesn't lock writes (HALOs
heartbeat every 2 min and we can't pause that). Alembic 1.10+ supports
CONCURRENTLY via execute() if autocommit mode is set in env.py — if not,
fall back to running the raw SQL manually:
  CREATE INDEX CONCURRENTLY ix_halo_readings_recent_desc
    ON halo_readings (device_id, metric, recorded_at DESC);

Revision ID: a138_halo_readings_desc_idx
Revises: a137_chat_team_write_perm
"""
from __future__ import annotations

from alembic import op


revision = "a138_halo_readings_desc_idx"
down_revision = "a137_chat_team_write_perm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain CREATE INDEX inside an Alembic txn (no CONCURRENTLY). The
    # already-built production index used CONCURRENTLY; this migration
    # is for fresh deployments where the table is empty/small and a
    # brief write lock is harmless. Raw SQL because op.create_index()
    # doesn't have first-class DESC-column support.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_halo_readings_recent_desc
        ON halo_readings (device_id, metric, recorded_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_halo_readings_recent_desc")
