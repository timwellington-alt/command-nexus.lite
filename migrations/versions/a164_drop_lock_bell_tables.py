"""Drop lock from door_state + retire the pre-consolidation bell tables.

Two cleanups in one migration since they share the same design pivot:

1. ``lock`` was functionally identical to neutral in the runner (both
   produce "not in an unlock window"). Removing it as a valid value
   simplifies the picker and the mental model. If a real "deny fob
   access" action is needed later, it will use a different Paxton
   call and warrant its own value name (probably ``deny``).

2. ``bell_periods`` / ``bell_calendar_overrides`` /
   ``bell_schedule_day_types`` were the pre-2026-08-17 parallel bell
   schedule I built before realizing CareHawk was authoritative.
   No code references them now — carehawk_calendar_cache is the
   only bell source.

Revision ID: a164_drop_lock_bell_tables
Revises: a163_door_group_building
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a164_drop_lock_bell_tables"
down_revision = "a163_door_group_building"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Tighten door_state to just NULL | 'unlock' ──
    #
    # First blank out any existing rows that were set to 'lock' — the
    # constraint drop-and-recreate would otherwise fail. Any 'lock'
    # tag was a no-op for the runner anyway.
    op.execute(
        "UPDATE carehawk_calendar_cache SET door_state = NULL "
        "WHERE door_state = 'lock'"
    )
    op.drop_constraint(
        "ck_carehawk_calendar_cache_door_state",
        "carehawk_calendar_cache", type_="check",
    )
    op.create_check_constraint(
        "ck_carehawk_calendar_cache_door_state",
        "carehawk_calendar_cache",
        "door_state IS NULL OR door_state = 'unlock'",
    )

    # ── 2. Drop the old parallel bell-schedule tables ──
    #
    # ``door_schedule_rules.day_type_id`` used to FK bell_schedule_day_types
    # — vestigial now (runner reads only the NULL-day-type row). Drop the
    # FK before the parent table can go. The column itself stays; a
    # future migration can drop it once we're sure no residual code
    # references it.
    op.drop_constraint(
        "door_schedule_rules_day_type_id_fkey",
        "door_schedule_rules", type_="foreignkey",
    )
    op.drop_table("bell_calendar_overrides")
    op.drop_table("bell_periods")
    op.execute("DROP INDEX IF EXISTS ix_bell_schedule_day_types_one_default")
    op.drop_table("bell_schedule_day_types")


def downgrade() -> None:
    # Recreate the old tables (from a159 / a161 schemas) so a rollback
    # gets you back to the pre-consolidation shape. Content is not
    # restored — this is an escape hatch, not a snapshot.
    op.create_table(
        "bell_schedule_day_types",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_bell_schedule_day_types_one_default "
        "ON bell_schedule_day_types (is_default) WHERE is_default"
    )
    op.create_table(
        "bell_periods",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("day_type_id", sa.Integer,
                  sa.ForeignKey("bell_schedule_day_types.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("starts_at", sa.Time, nullable=False),
        sa.Column("ends_at", sa.Time, nullable=False),
        sa.Column("door_state", sa.String(10)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("day_type_id", "seq", name="uq_bell_periods_day_seq"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_bell_periods_end_gt_start"),
    )
    op.create_table(
        "bell_calendar_overrides",
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("day_type_id", sa.Integer,
                  sa.ForeignKey("bell_schedule_day_types.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("updated_by", sa.String(255)),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
    )

    op.drop_constraint(
        "ck_carehawk_calendar_cache_door_state",
        "carehawk_calendar_cache", type_="check",
    )
    op.create_check_constraint(
        "ck_carehawk_calendar_cache_door_state",
        "carehawk_calendar_cache",
        "door_state IS NULL OR door_state IN ('unlock', 'lock')",
    )
