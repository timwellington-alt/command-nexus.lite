"""Per-event door open/close offsets (lead/lag time).

Positive seconds delay, negative seconds fire early. Applied on top of
whatever start/end times the pair-aware runtime resolves for a window,
so operators can nudge a single event without touching the group-wide
default in door_schedule_rules.

Revision ID: a175_event_door_offsets
Revises: a174_carehawk_snapshots
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a175_event_door_offsets"
down_revision = "a174_carehawk_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "carehawk_calendar_cache",
        sa.Column("door_start_offset_sec", sa.Integer, nullable=False,
                  server_default="0"),
    )
    op.add_column(
        "carehawk_calendar_cache",
        sa.Column("door_end_offset_sec", sa.Integer, nullable=False,
                  server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("carehawk_calendar_cache", "door_end_offset_sec")
    op.drop_column("carehawk_calendar_cache", "door_start_offset_sec")
