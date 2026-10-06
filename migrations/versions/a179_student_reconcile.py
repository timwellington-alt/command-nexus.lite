"""Student roster ↔ Google reconciliation tables.

Two failure modes today's data revealed:
  - Active-in-SIS kids drifted to /Archived Accounts (14 confirmed) — no
    Nexus audit trail; external process moved them. Diff-based pipeline
    only revisits kids in an "added" burst so it never notices silent
    drift on the Google side.
  - Kids withdrawn/graduated years ago still active in Google. Auto-
    deprovision only fires on the per-import "withdrawn since yesterday"
    diff. Miss the withdrawal day → the kid is permanently invisible.

This migration adds the two tables the nightly reconciliation job writes
to: a per-run summary and a per-account decision log. Also inserts default
settings rows so the job is DISABLED and DRY-RUN by default — operator has
to flip both switches in /settings before any Google writes happen.

Revision ID: a179_student_reconcile
Revises: a178_event_silence_at_device
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a179_student_reconcile"
down_revision = "a178_event_silence_at_device"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_reconcile_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("aborted_reason", sa.Text, nullable=True),
        sa.Column("roster_active_count", sa.Integer, nullable=True),
        sa.Column("dir_a_scanned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dir_a_candidates", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dir_a_applied", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dir_b_scanned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dir_b_candidates", sa.Integer, nullable=False, server_default="0"),
        sa.Column("dir_b_applied", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer, nullable=False, server_default="0"),
        sa.Column("notes", postgresql.JSONB, nullable=True),
    )
    op.create_index(
        "ix_student_reconcile_runs_started_at",
        "student_reconcile_runs", ["started_at"], unique=False,
    )

    op.create_table(
        "student_reconcile_candidates",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("run_id", sa.Integer,
                  sa.ForeignKey("student_reconcile_runs.id",
                                ondelete="CASCADE"),
                  nullable=False),
        sa.Column("direction", sa.String(1), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("sis_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("roster_school", sa.String(16), nullable=True),
        sa.Column("google_ou", sa.String(200), nullable=True),
        sa.Column("target_ou", sa.String(200), nullable=True),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("google_creation", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_reconcile_cand_run",
        "student_reconcile_candidates", ["run_id"], unique=False,
    )
    op.create_index(
        "ix_reconcile_cand_email",
        "student_reconcile_candidates", ["email"], unique=False,
    )
    op.create_index(
        "ix_reconcile_cand_direction_decision",
        "student_reconcile_candidates", ["direction", "decision"], unique=False,
    )

    # Insert conservative defaults into integration_settings so the job
    # is inert until an operator explicitly flips the switches. Do not
    # overwrite anything already present.
    op.execute("""
        INSERT INTO integration_configs (integration, key, value, is_secret_ref, updated_by, updated_at)
        VALUES
          ('roster', 'reconcile_enabled',            'false', false, 'migration:a179', NOW()),
          ('roster', 'reconcile_dry_run',            'true',  false, 'migration:a179', NOW()),
          ('roster', 'reconcile_stale_days',         '30',    false, 'migration:a179', NOW()),
          ('roster', 'reconcile_lastlogin_min_days', '180',   false, 'migration:a179', NOW()),
          ('roster', 'reconcile_max_per_run',        '50',    false, 'migration:a179', NOW()),
          ('roster', 'reconcile_min_roster_active',  '1500',  false, 'migration:a179', NOW())
        ON CONFLICT (integration, key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM integration_configs
        WHERE integration='roster' AND key IN (
          'reconcile_enabled','reconcile_dry_run','reconcile_stale_days',
          'reconcile_lastlogin_min_days','reconcile_max_per_run',
          'reconcile_min_roster_active'
        )
    """)
    op.drop_index("ix_reconcile_cand_direction_decision", "student_reconcile_candidates")
    op.drop_index("ix_reconcile_cand_email", "student_reconcile_candidates")
    op.drop_index("ix_reconcile_cand_run", "student_reconcile_candidates")
    op.drop_table("student_reconcile_candidates")
    op.drop_index("ix_student_reconcile_runs_started_at", "student_reconcile_runs")
    op.drop_table("student_reconcile_runs")
