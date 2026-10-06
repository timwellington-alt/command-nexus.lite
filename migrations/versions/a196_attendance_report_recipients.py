"""Attendance report recipients — per-building email digest list.

Standalone from the routed-alert system (voice_recipients). Purely
opt-in daily digest — no severity thresholds, no quiet hours, no
per-channel toggles. One row per (building, email); a recipient can
subscribe to multiple buildings by adding multiple rows.

Empty building_code = district-wide roll-up (all schools in one email).

Revision ID: a196_att_report_recips
Revises: a195_roster_analytics
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a196_att_report_recips"
down_revision = "a195_roster_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attendance_report_recipients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("building_code", sa.String(20), nullable=False, server_default=""),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("notes", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.String(255), nullable=True),
    )
    op.create_index(
        "ux_attendance_report_recipients",
        "attendance_report_recipients",
        ["building_code", "email"],
        unique=True,
    )

    op.execute("""
        INSERT INTO permissions (action, description)
        VALUES ('roster.attendance_reports.manage',
                'Manage attendance report email recipients (per-building daily digest subscription)')
        ON CONFLICT (action) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM permissions WHERE action = 'roster.attendance_reports.manage'")
    op.drop_index("ux_attendance_report_recipients", table_name="attendance_report_recipients")
    op.drop_table("attendance_report_recipients")
