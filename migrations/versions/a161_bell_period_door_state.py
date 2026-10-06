"""Explicit door_state on bell_periods.

Design pivot after Phase 3/4 landed with the implicit "passing
window = gap between periods" model. Ambiguity there was: an admin
looking at the bell schedule couldn't tell at a glance whether the
door scheduler treated a given gap as an unlock window (it did, if
the gap was long enough). Now each period row carries an explicit
door_state, and the door scheduler acts on that label — no
derivation, no guessing.

Values:
  NULL       → neutral. No door state change during this period.
  'unlock'   → hold configured doors open during this period.
  'lock'     → close configured doors during this period.

Existing rows stay NULL so nothing changes behaviorally until an
admin edits them (or uses the auto-fill-passing helper on the
bell schedule page).

Revision ID: a161_bell_period_door_state
Revises: a160_door_schedule
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a161_bell_period_door_state"
down_revision = "a160_door_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bell_periods",
        sa.Column("door_state", sa.String(10), nullable=True),
    )
    op.create_check_constraint(
        "ck_bell_periods_door_state",
        "bell_periods",
        "door_state IS NULL OR door_state IN ('unlock', 'lock')",
    )
    # Partial index — the door scheduler queries "give me the
    # unlock/lock periods for day type X". Skipping NULLs (the common
    # case for class-time rows) keeps this cheap.
    op.execute(
        "CREATE INDEX ix_bell_periods_door_state "
        "ON bell_periods (day_type_id, door_state) "
        "WHERE door_state IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_bell_periods_door_state")
    op.drop_constraint("ck_bell_periods_door_state", "bell_periods", type_="check")
    op.drop_column("bell_periods", "door_state")
