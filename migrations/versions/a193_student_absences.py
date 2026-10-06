"""Student absence facts — MetaSolutions "Daily Attendance" ingest target.

Stores one row per (student, calendar_date, absence_type) so the
common case of a full-day unexcused absence is a single row, but
period-level or multi-type events on the same day (e.g. tardy AM +
early dismissal PM) get their own rows without a schema change.

Field naming: the MetaSolutions CSV uses `<Name>2` suffixes across the
board (StudentNumber2, LastName2, etc. — presumably a legacy join
artifact from their SQL report). We strip the `2` in normalized column
names since it carries no information.

Retention: **forever**. Absences are historical facts; the analytics
dashboard needs multi-year comparisons and Ohio compliance
(chronic-absence tracking) reaches back a full school year. Size at
~500 absences/day × 180 school days × 5 years ≈ 450k rows, trivial.

Revision ID: a193_student_absences
Revises: a192_clever_verify
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a193_student_absences"
down_revision = "a192_clever_verify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_absences",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("sis_id", sa.String(50), nullable=False),
        sa.Column("school_code", sa.String(20), nullable=False),
        sa.Column("calendar_date", sa.Date, nullable=False),
        # Absence taxonomy (from CSV). `absence_type` is the short code
        # (U, E, T, etc.); `absence_type_name` and `absence_level`
        # ("Unexcused" / "Full Absence") are the human-readable forms.
        sa.Column("absence_type", sa.String(10), nullable=False),
        sa.Column("absence_type_name", sa.String(60), nullable=True),
        sa.Column("absence_level", sa.String(60), nullable=True),
        sa.Column("absence_reason", sa.String(120), nullable=True),
        sa.Column("absence_note", sa.Text, nullable=True),
        # Time-in/out for partial-day absences (blank on full-day rows).
        sa.Column("time_in", sa.String(20), nullable=True),
        sa.Column("time_out", sa.String(20), nullable=True),
        sa.Column("comments", sa.Text, nullable=True),
        # Snapshot fields at time of import — the roster changes across
        # a school year (grade, homeroom, contact info) but the absence
        # is anchored to what was true THEN. Denormalized so historical
        # reports don't need a join-on-date-range against roster_snapshots.
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("grade", sa.String(10), nullable=True),
        sa.Column("homeroom", sa.String(120), nullable=True),
        sa.Column("primary_contact_name", sa.String(200), nullable=True),
        sa.Column("primary_contact_phone", sa.String(30), nullable=True),
        # Ingest provenance.
        sa.Column("source_message_id", sa.String(120), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Upsert key — MetaSolutions re-runs the report backfilling prior
    # dates, so a same-date same-student same-type row must UPDATE
    # (comments/notes may have been added) rather than INSERT twice.
    op.create_index(
        "ux_student_absences_key",
        "student_absences",
        ["sis_id", "calendar_date", "absence_type"],
        unique=True,
    )
    # Query indexes for the common lookups
    op.create_index(
        "ix_student_absences_sis_date",
        "student_absences",
        ["sis_id", "calendar_date"],
    )
    op.create_index(
        "ix_student_absences_school_date",
        "student_absences",
        ["school_code", "calendar_date"],
    )
    op.create_index(
        "ix_student_absences_date",
        "student_absences",
        ["calendar_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_student_absences_date", table_name="student_absences")
    op.drop_index("ix_student_absences_school_date", table_name="student_absences")
    op.drop_index("ix_student_absences_sis_date", table_name="student_absences")
    op.drop_index("ux_student_absences_key", table_name="student_absences")
    op.drop_table("student_absences")
