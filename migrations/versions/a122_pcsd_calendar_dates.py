"""Central district school calendar table.

One row per date in the school year (including weekends). Other modules
ask this table "is today a student day?" / "what grading period are we
in?" instead of each implementing its own calendar logic.

Revision ID: a122_district_calendar_dates
Revises: a121_paxton_acu_name
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a122_district_calendar_dates"
down_revision = "a121_paxton_acu_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "district_calendar_dates",
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("school_year", sa.String(9), nullable=False),
        sa.Column("day_type", sa.String(40), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("is_student_day", sa.Boolean, nullable=False),
        sa.Column("is_teacher_day", sa.Boolean, nullable=False),
        sa.Column("grading_period", sa.String(10), nullable=True),
        sa.Column("in_testing_window", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_district_calendar_dates_year", "district_calendar_dates", ["school_year"])


def downgrade() -> None:
    op.drop_index("ix_district_calendar_dates_year", table_name="district_calendar_dates")
    op.drop_table("district_calendar_dates")
