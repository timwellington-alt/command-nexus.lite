"""Persist provisioning run outcomes (per-student details) so gaps
between what the diff pipeline THINKS it did and what actually landed
in Google can be traced after the fact.

Prior to this: prov_result["details"] was thrown away after the job
returned. On 2026-08-20 both Briggs and Hersey were flagged reactivated/
auto_linked in an import that reported errors=0, but the underlying
Google accounts didn't exist 3 days later — no audit trail to see what
happened. This row-per-run + full details JSONB gives us that trail.

Revision ID: a176_roster_provisioning_runs
Revises: a175_event_door_offsets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a176_roster_provisioning_runs"
down_revision = "a175_event_door_offsets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roster_provisioning_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ran_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(200), nullable=True),
        sa.Column("provisioned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reactivated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("review_flagged", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer, nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_reason", sa.Text, nullable=True),
        sa.Column("candidate_count", sa.Integer, nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=True),
    )
    op.create_index(
        "ix_roster_provisioning_runs_ran_at",
        "roster_provisioning_runs", ["ran_at"], unique=False,
    )
    op.create_index(
        "ix_roster_provisioning_runs_source_ran_at",
        "roster_provisioning_runs", ["source", "ran_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_roster_provisioning_runs_source_ran_at", "roster_provisioning_runs")
    op.drop_index("ix_roster_provisioning_runs_ran_at", "roster_provisioning_runs")
    op.drop_table("roster_provisioning_runs")
