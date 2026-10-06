"""Per-run history for scheduled jobs.

`job_health` tracks cumulative counters (last_success_at, total_success,
etc.) which are great for check_job_liveness but useless for
retrospective debugging ("why did backup_switch_configs fail three times
last night?"). This adds a slim per-run table so /operations/health can
drill into recent runs of any job.

Retention is capped in the recorder itself (keep newest N per job so
this doesn't balloon). No FK to job_health — the two are independent
observability tables that never JOIN in the hot path.

Revision ID: a200_job_runs
Revises: a199_cast_camera_streams
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a200_job_runs"
down_revision = "a199_cast_camera_streams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_name", sa.String(80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("duration_sec", sa.Numeric(10, 3)),
        sa.Column("error", sa.Text),
    )
    op.create_index(
        "ix_job_runs_name_started",
        "job_runs",
        ["job_name", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_job_runs_name_started", table_name="job_runs")
    op.drop_table("job_runs")
