"""Scope bell event door control to specific door groups.

Prior model (a162): each cached CareHawk event had a single
``door_state`` = ``unlock`` | ``null`` flag. The door scheduler
would hold open **every** door group bound to the event's building.
That means an "errant" unlock tag at PHS would fire every PHS group,
including ones the operator didn't intend.

New model: each event has a JSONB array ``door_group_ids`` of
door_schedule_groups.id values to hold open during the window.
Presence in the array = intent to unlock; empty array = no effect.
This lets operators say "unlock PHS Foyer during Passing 1→2 but
not the athletic doors."

Referential integrity is soft — deleting a door group leaves stale
ids in the JSONB, which the runner silently skips. Deliberate: full
FK would need an association table + join, more moving parts than
this needs for a low-volume tag.

Revision ID: a165_event_door_group_scope
Revises: a164_drop_lock_bell_tables
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a165_event_door_group_scope"
down_revision = "a164_drop_lock_bell_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New column — default '[]' so existing rows are "no effect"
    # (matches the prior door_state=NULL semantics safely).
    op.add_column(
        "carehawk_calendar_cache",
        sa.Column(
            "door_group_ids",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # GIN index — runner queries "is group X in this array?" via the
    # ``?`` / ``@>`` operators. GIN makes those cheap even at scale.
    op.execute(
        "CREATE INDEX ix_carehawk_calendar_cache_door_group_ids "
        "ON carehawk_calendar_cache USING GIN (door_group_ids)"
    )

    # Retire the old boolean door_state field. Any 'unlock' tag that
    # existed pre-migration is dropped rather than auto-converted —
    # the operator has to explicitly pick which groups to unlock now,
    # which is the whole point of this migration.
    op.execute("DROP INDEX IF EXISTS ix_carehawk_calendar_cache_unlock")
    op.drop_constraint(
        "ck_carehawk_calendar_cache_door_state",
        "carehawk_calendar_cache", type_="check",
    )
    op.drop_column("carehawk_calendar_cache", "door_state")


def downgrade() -> None:
    op.add_column(
        "carehawk_calendar_cache",
        sa.Column("door_state", sa.String(10), nullable=True),
    )
    op.create_check_constraint(
        "ck_carehawk_calendar_cache_door_state",
        "carehawk_calendar_cache",
        "door_state IS NULL OR door_state = 'unlock'",
    )
    op.execute(
        "CREATE INDEX ix_carehawk_calendar_cache_unlock "
        "ON carehawk_calendar_cache (building_id, door_state) "
        "WHERE door_state IS NOT NULL"
    )
    op.execute("DROP INDEX IF EXISTS ix_carehawk_calendar_cache_door_group_ids")
    op.drop_column("carehawk_calendar_cache", "door_group_ids")
