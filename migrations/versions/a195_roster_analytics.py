"""Roster analytics — daily rollup + provisioning SLA events + permission.

Broader multi-domain rollup for the /roster/analytics dashboard.
Complements a194's attendance-only tables:

  roster_analytics_daily
      One row per (snapshot_date, building_code, grade).
      building_code='' + grade='' rows = district-wide totals.
      building_code=<code> + grade='' = building-wide totals.
      building_code=<code> + grade=<grade> = per-grade cell.
      Idempotent nightly rebuild.

  roster_analytics_sla_events
      One row per provision or archive lifecycle event.
      sis_event_at = when SIS reported the add/remove.
      google_event_at = when Nexus acted (NULL = still pending).
      sla_seconds = google_event_at - sis_event_at (NULL if pending).
      Powers the SLA distribution charts in the Google-lifecycle tab.

Also inserts the roster.analytics.view permission so
require_action("roster.analytics.view") gates dashboard access.

Retention: forever. Size math per plan: ~11 MB/year.

Revision ID: a195_roster_analytics
Revises: a194_attendance_analytics
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a195_roster_analytics"
down_revision = "a194_attendance_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roster_analytics_daily",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column("building_code", sa.String(20), nullable=False, server_default=""),
        sa.Column("grade", sa.String(10), nullable=False, server_default=""),
        # Roster
        sa.Column("enrolled_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("added_today", sa.Integer, nullable=False, server_default="0"),
        sa.Column("removed_today", sa.Integer, nullable=False, server_default="0"),
        sa.Column("transferred_today", sa.Integer, nullable=False, server_default="0"),
        # Google lifecycle
        sa.Column("google_active", sa.Integer, nullable=False, server_default="0"),
        sa.Column("google_suspended", sa.Integer, nullable=False, server_default="0"),
        sa.Column("google_archived", sa.Integer, nullable=False, server_default="0"),
        sa.Column("google_missing", sa.Integer, nullable=False, server_default="0"),
        sa.Column("logged_in_7d", sa.Integer, nullable=False, server_default="0"),
        sa.Column("logged_in_30d", sa.Integer, nullable=False, server_default="0"),
        # Chromebook — only populated on building-rollup rows (grade='')
        sa.Column("cb_assigned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cb_avg_age_days", sa.Integer),
        sa.Column("cb_repairs_30d", sa.Integer, nullable=False, server_default="0"),
        # Anomaly
        sa.Column("import_anomaly_flag", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ux_roster_analytics_daily",
        "roster_analytics_daily",
        ["snapshot_date", "building_code", "grade"],
        unique=True,
    )
    op.create_index(
        "ix_roster_analytics_daily_date",
        "roster_analytics_daily",
        ["snapshot_date"],
    )

    op.create_table(
        "roster_analytics_sla_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("sis_id", sa.String(50), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("sis_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("google_event_at", sa.DateTime(timezone=True)),
        sa.Column("sla_seconds", sa.Integer),
    )
    op.create_index(
        "ix_sla_events_type_date",
        "roster_analytics_sla_events",
        ["event_type", "sis_event_at"],
    )
    # Same (sis_id, event_type, sis_event_at) can't dupe — needed so
    # the backfill can re-run without inflating counts.
    op.create_index(
        "ux_sla_events_dedup",
        "roster_analytics_sla_events",
        ["sis_id", "event_type", "sis_event_at"],
        unique=True,
    )

    op.execute("""
        INSERT INTO permissions (action, description)
        VALUES ('roster.analytics.view',
                'View the Roster Analytics dashboard (enrollment, Google lifecycle, capacity, engagement, anomaly)')
        ON CONFLICT (action) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM permissions WHERE action = 'roster.analytics.view'")
    op.drop_index("ux_sla_events_dedup", table_name="roster_analytics_sla_events")
    op.drop_index("ix_sla_events_type_date", table_name="roster_analytics_sla_events")
    op.drop_table("roster_analytics_sla_events")
    op.drop_index("ix_roster_analytics_daily_date", table_name="roster_analytics_daily")
    op.drop_index("ux_roster_analytics_daily", table_name="roster_analytics_daily")
    op.drop_table("roster_analytics_daily")
