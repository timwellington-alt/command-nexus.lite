"""Per-event door_action_timing so the operator can pick when to
unlock relative to the event's own time.

Values:
  ``during``  — hold doors open from start_time to end_time. Right for
                span-style events like breakfast/lunch/tardy grace.
  ``at_end``  — hold doors open at the event's end_time, close at the
                next chronological bell's start_time. Right for
                regular class-period bells where you want the
                passing window unlocked without having to also define
                a Passing row.
  NULL        — no explicit choice. Runner falls back to ``during``
                (safe default: unlock spans the event itself).

The prior ``pair_events`` Start/Stop convention is now vestigial —
kept in the codebase for legacy schedules but no longer required
for the runner to work. Each event is standalone; the runner
computes windows from timing + chronological next-bell lookup.

Revision ID: a166_door_action_timing
Revises: a165_event_door_group_scope
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a166_door_action_timing"
down_revision = "a165_event_door_group_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "carehawk_calendar_cache",
        sa.Column("door_action_timing", sa.String(20), nullable=True),
    )
    op.create_check_constraint(
        "ck_carehawk_calendar_cache_door_action_timing",
        "carehawk_calendar_cache",
        "door_action_timing IS NULL OR door_action_timing IN ('during', 'at_end')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_carehawk_calendar_cache_door_action_timing",
        "carehawk_calendar_cache", type_="check",
    )
    op.drop_column("carehawk_calendar_cache", "door_action_timing")
