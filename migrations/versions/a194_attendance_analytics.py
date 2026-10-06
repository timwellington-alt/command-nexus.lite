"""Attendance analytics: per-student chronic flag + per-school daily stats.

Two focused tables that power today's Phase C outputs (Ohio chronic-
absence flag on each active student + a daily per-school rate for
spike detection + Phase D trend charts). Deliberately narrower than
the full multi-domain rollup called for in the roster-analytics plan
— that broader `roster_analytics_daily` lands in Phase D alongside
the dashboard consumer.

Tables:
  student_analytics
      One row per active student. Rewritten nightly by
      snapshot_attendance_analytics. Chronic flag = Ohio's definition:
      absent (any type) 10%+ of enrolled school days year-to-date.

  attendance_daily_stats
      One row per (date, school_code). Total absences + enrolled count
      + rate. Powers the spike-alert delta (today vs 30-day baseline)
      and the per-school trend line on the analytics page.

Retention: forever on daily_stats (aligns with forever retention on
student_absences); student_analytics is a rolling snapshot (overwrite
per student), no history table needed since the daily_stats table
captures the trend.

Revision ID: a194_attendance_analytics
Revises: a193_student_absences
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a194_attendance_analytics"
down_revision = "a193_student_absences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_analytics",
        sa.Column("sis_id", sa.String(50), primary_key=True),
        sa.Column("school_code", sa.String(20), nullable=True),
        sa.Column("grade", sa.String(10), nullable=True),
        sa.Column("days_absent_ytd", sa.Integer, nullable=False, server_default="0"),
        sa.Column("days_enrolled_ytd", sa.Integer, nullable=False, server_default="0"),
        # Rate stored as percentage, 0-100, with 2 decimals precision
        sa.Column("absence_rate_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        # Ohio chronic absenteeism = missed 10%+ of enrolled days
        sa.Column("chronic_absent", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_student_analytics_chronic",
        "student_analytics",
        ["chronic_absent"],
        postgresql_where=sa.text("chronic_absent = true"),
    )
    op.create_index(
        "ix_student_analytics_school_grade",
        "student_analytics",
        ["school_code", "grade"],
    )

    op.create_table(
        "attendance_daily_stats",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("calendar_date", sa.Date, nullable=False),
        sa.Column("school_code", sa.String(20), nullable=False),
        sa.Column("total_absences", sa.Integer, nullable=False, server_default="0"),
        sa.Column("distinct_students_absent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enrolled_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("absence_rate_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ux_attendance_daily_stats_key",
        "attendance_daily_stats",
        ["calendar_date", "school_code"],
        unique=True,
    )
    op.create_index(
        "ix_attendance_daily_stats_date",
        "attendance_daily_stats",
        ["calendar_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_attendance_daily_stats_date", table_name="attendance_daily_stats")
    op.drop_index("ux_attendance_daily_stats_key", table_name="attendance_daily_stats")
    op.drop_table("attendance_daily_stats")
    op.drop_index("ix_student_analytics_school_grade", table_name="student_analytics")
    op.drop_index("ix_student_analytics_chronic", table_name="student_analytics")
    op.drop_table("student_analytics")
