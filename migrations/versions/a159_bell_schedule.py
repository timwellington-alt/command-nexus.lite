"""Bell schedule primitive — day types, periods, calendar overrides.

Foundation for the PHS foyer-door scheduler + future features that
need to reason about "which class period are we in right now?" (sub
notifications, cleaning windows, tardy alerts, HVAC setbacks).

Three tables:
  bell_schedule_day_types — the finite set of schedule flavors
    (regular / two_hour_delay / early_dismissal / assembly / no_school).
    Seeded on migration up. Exactly one row has is_default=true, and
    that row is used any time the calendar doesn't say otherwise.

  bell_periods — the actual class blocks for each day type. Passing
    windows are derived from gaps between consecutive periods; no
    separate rows for passing. TIME-typed (local wall-clock).

  bell_calendar_overrides — one row per date that overrides the
    default day type. E.g. a February 15 blizzard entry pointing at
    two_hour_delay, or a June 4 last-day entry pointing at
    early_dismissal.

Revision ID: a159_bell_schedule
Revises: a158_onboarding_tokens
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a159_bell_schedule"
down_revision = "a158_onboarding_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bell_schedule_day_types",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # Short code used programmatically. UNIQUE so runner code can
        # look up by code without ambiguity.
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text),
        # Partial unique index enforces exactly one default row —
        # simpler than a check constraint spanning multiple rows.
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
        # Display order — periods are shown/iterated by seq, not id.
        # Gaps between consecutive seq values inside the same day_type
        # are the passing windows the door scheduler acts on.
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        # Local wall-clock — TIME (no zone). The runner does the tz
        # conversion when comparing to now(). Storing tz-aware here
        # would be wrong: 8:00 AM means the same wall-clock time on a
        # DST boundary day as any other day.
        sa.Column("starts_at", sa.Time, nullable=False),
        sa.Column("ends_at", sa.Time, nullable=False),
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
    # Reverse lookup: "show me every date that uses day_type X".
    op.create_index(
        "ix_bell_calendar_overrides_day_type",
        "bell_calendar_overrides", ["day_type_id"],
    )

    # ── Seed the five day types ──
    #
    # Every district has these five conceptual buckets. Codes are stable;
    # names are user-editable via the admin UI. `regular` is the default.
    op.execute("""
        INSERT INTO bell_schedule_day_types (code, name, description, is_default) VALUES
          ('regular',         'Regular Day',      'Standard bell schedule',
             true),
          ('two_hour_delay',  '2-Hour Delay',     'Late start — first period begins 2 hours after normal',
             false),
          ('early_dismissal', 'Early Dismissal',  'Shortened day — early release',
             false),
          ('assembly',        'Assembly Day',     'Shortened periods to accommodate an assembly block',
             false),
          ('no_school',       'No School',        'Holiday, break, or closure — no periods scheduled',
             false);
    """)


def downgrade() -> None:
    op.drop_index("ix_bell_calendar_overrides_day_type", table_name="bell_calendar_overrides")
    op.drop_table("bell_calendar_overrides")
    op.drop_table("bell_periods")
    op.execute("DROP INDEX IF EXISTS ix_bell_schedule_day_types_one_default")
    op.drop_table("bell_schedule_day_types")
