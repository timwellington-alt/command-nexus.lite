"""Per-job liveness tracking so we can alert on stale/broken scheduled jobs.

Root cause of the 2026-08-19/20 Clever outage: my a169/a170 damage-report
relationships broke SQLAlchemy mapper initialization inside the ARQ
worker, and every subsequent scheduled ORM query silently failed for
two days. Nothing detected it because ``health_check_job`` only
tested raw DB/Redis connectivity, not job outcomes.

This table gets stamped by an ARQ ``on_job_end`` hook on every job
completion. A dedicated ``check_job_liveness`` job compares each
scheduled job's ``last_success_at`` against its expected cadence
and pages via ``dispatch_routed_alert`` when jobs go stale.

Revision ID: a172_job_health
Revises: a171_student_teachers_term_name
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a172_job_health"
down_revision = "a171_student_teachers_term_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_health",
        sa.Column("job_name", sa.String(80), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_result_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer,
                  nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("last_duration_sec", sa.Numeric(10, 3)),
        sa.Column("total_success", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("total_failure", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.create_index("ix_job_health_last_result", "job_health", ["last_result_at"])


def downgrade() -> None:
    op.drop_table("job_health")
