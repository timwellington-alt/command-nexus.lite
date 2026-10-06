"""Student membership status (authoritative SIS-side enrollment code).

Two tables:

  student_membership_status — one row per student, populated from the
    "Memberships All Yrs All Info" CSV (MetaSolutions morning report).
    Deduped on StudentNumber; every row of the source CSV for one kid
    carries the same StudentStatus, so we keep just one.

  student_attends_our_classes — per-student override flag, mirrors
    roster_deprov_exemptions. For A/E-ish codes (R, CP, F, CTC, etc.)
    where SOME kids actually take classes at the district, this flag says
    "treat this specific kid as enrolled regardless of the code."
    Same opt-in pattern as "Protect from sweep."

Effective-enrolled = code.enrolled OR sis_id IN attends_our_classes.
Downstream consumers (guidance auto-close, deprovision guard, roster
analytics) will read this instead of inferring from roster_snapshots.status.

Revision ID: a201_student_membership
Revises: a200_job_runs
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a201_student_membership"
down_revision = "a200_job_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_membership_status",
        sa.Column("sis_id", sa.String(64), primary_key=True),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("district_withdrawal_date", sa.String(20)),
        sa.Column("district_withdrawal_reason", sa.String(20)),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("source_subject", sa.Text),
    )
    op.create_index(
        "ix_student_membership_status_code",
        "student_membership_status", ["code"],
    )

    op.create_table(
        "student_attends_our_classes",
        sa.Column("sis_id", sa.String(64), primary_key=True),
        sa.Column("reason", sa.Text),
        sa.Column("added_by", sa.String(255), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("notes", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("student_attends_our_classes")
    op.drop_index("ix_student_membership_status_code",
                  table_name="student_membership_status")
    op.drop_table("student_membership_status")
