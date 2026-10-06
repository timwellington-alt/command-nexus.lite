"""Named, per-building bell-schedule snapshots — server-side backups.

Complements the browser localStorage draft (short-term crash recovery)
with proper server-side backups that survive browser changes and
cross-device work. Every push auto-creates a snapshot of the
PRIOR state, so a bad push is one click away from being undone.
Manual snapshots (named) never auto-prune; auto-snapshots keep the
most recent 20 per building.

Revision ID: a174_carehawk_snapshots
Revises: a173_alert_module_overrides
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a174_carehawk_snapshots"
down_revision = "a173_alert_module_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "carehawk_calendar_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("building_id", sa.Integer, nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        # 'manual' = user-saved, kept forever
        # 'auto'   = pre-push auto-snapshot, pruned to last 20/bldg
        sa.Column("kind", sa.String(20), nullable=False, server_default="manual"),
        # Full CalendarDTO payload (normal_day_events + templates +
        # per-event door_group_ids + door_action_timing). Stored as
        # JSONB so future features can query into it (e.g. "which
        # snapshots have this event signature").
        sa.Column("payload", postgresql.JSONB, nullable=False),
        # Handy denorm counters so the list UI doesn't have to parse
        # payload just to show "N events".
        sa.Column("event_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("template_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_carehawk_snapshots_building_kind_created",
        "carehawk_calendar_snapshots",
        ["building_id", "kind", "created_at"],
    )
    op.create_check_constraint(
        "ck_carehawk_snapshots_kind",
        "carehawk_calendar_snapshots",
        "kind IN ('manual', 'auto')",
    )


def downgrade() -> None:
    op.drop_table("carehawk_calendar_snapshots")
