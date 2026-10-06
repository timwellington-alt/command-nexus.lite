"""Door scheduler — groups + members + per-day-type rules.

Sits on top of the bell schedule primitive (a159_bell_schedule):
each group's members (Paxton door_ids) get held open during the
passing windows derived from bell_periods, snapped closed at the
next period bell.

Three tables:

  door_schedule_groups — a named set of doors that share a schedule.
    Enabled defaults FALSE so the migration doesn't accidentally
    start driving doors on next tick; operator flips enabled=true
    from the admin UI when they're ready.

  door_schedule_group_members — the door_ids in each group. Uses
    the Paxton door_id (not MAC) since that's what
    PaxtonAdapter.hold_open / close_door take. Cached door name is
    denormalized so the UI can show "PHS-Foyer-Art-Hall" without a
    live Paxton API round-trip on every render.

  door_schedule_rules — per-day-type behavior. day_type_id NULL means
    "apply to every day type this group is scheduled for", which is
    the normal case (foyers open in all passing windows on any bell
    day). Rows with an explicit day_type_id are the escape hatch —
    e.g. "don't hold open on assembly days" would be a row with
    max_hold_duration_sec=0.

Seeds the "PHS Foyer" group with the 4 door_ids Tim confirmed
2026-08-17. Group enabled=FALSE by design — operator must flip
consciously once bells are entered.

Revision ID: a160_door_schedule
Revises: a159_bell_schedule
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a160_door_schedule"
down_revision = "a159_bell_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "door_schedule_groups",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        # Defaults FALSE so a fresh migration doesn't start driving
        # doors on the next scheduler tick. Admin flips this after
        # entering bells + confirming the member list.
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("description", sa.Text),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
    )

    op.create_table(
        "door_schedule_group_members",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Integer,
                  sa.ForeignKey("door_schedule_groups.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # Paxton door_id (bigint — matches paxton_acu_status.door_id).
        # This is what PaxtonAdapter.hold_open / close_door take.
        sa.Column("paxton_door_id", sa.BigInteger, nullable=False),
        # Denormalized snapshot at time-of-add so the admin UI has
        # something to show even if paxton_acu_status is briefly stale.
        # Refreshed by the /api endpoint whenever the group is opened.
        sa.Column("cached_door_name", sa.String(255)),
        sa.Column("added_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("group_id", "paxton_door_id",
                            name="uq_door_group_members_group_door"),
    )

    op.create_table(
        "door_schedule_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Integer,
                  sa.ForeignKey("door_schedule_groups.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # NULL = applies to every day type. Setting a specific
        # day_type_id lets you override behavior for e.g. assembly
        # days (max_hold_duration_sec=0 → no holds that day).
        sa.Column("day_type_id", sa.Integer,
                  sa.ForeignKey("bell_schedule_day_types.id", ondelete="CASCADE"),
                  nullable=True),
        # How many seconds after a period ends the door should open.
        # 0 = open at bell. Positive = wait N seconds (unusual — kids
        # get to the door slower than the bell). Negative = open before
        # the bell (also unusual — anticipate).
        sa.Column("hold_open_start_offset_sec", sa.Integer,
                  server_default=sa.text("0"), nullable=False),
        # How many seconds after the NEXT period starts the door
        # should close. 0 = close at bell. Positive = kids get a grace
        # window (typical for schools: close 30s after bell to catch
        # stragglers). Negative = close before bell (rare).
        sa.Column("hold_open_end_offset_sec", sa.Integer,
                  server_default=sa.text("0"), nullable=False),
        # Safety floor: refuse to hold open for less than this. Guards
        # against a zero-length passing window (e.g. back-to-back
        # periods) triggering a rapid open/close jitter.
        sa.Column("min_hold_duration_sec", sa.Integer,
                  server_default=sa.text("60"), nullable=False),
        # Safety ceiling: the outer bound Tim asked for. If the runner
        # somehow misses the close call, the door won't stay held
        # longer than this. Set to 0 to explicitly disable holds for
        # this rule (useful for the "no holds on assembly days" case).
        sa.Column("max_hold_duration_sec", sa.Integer,
                  server_default=sa.text("900"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("group_id", "day_type_id",
                            name="uq_door_schedule_rules_group_daytype"),
    )

    # ── Seed: PHS Foyer group ──
    #
    # Includes the 4 Foyer door_ids Tim confirmed on 2026-08-17.
    # Enabled=false — operator flips after bells are entered.
    op.execute("""
        INSERT INTO door_schedule_groups (name, enabled, description, created_by)
        VALUES ('PHS Foyer', false,
                'Internal hallway doors that stay unlocked during class-change windows. Requested by SRO.',
                'system')
    """)
    op.execute("""
        INSERT INTO door_schedule_group_members (group_id, paxton_door_id, cached_door_name)
        SELECT g.id, x.door_id, x.name FROM door_schedule_groups g,
            (VALUES
                (9213502::bigint, 'PHS-Foyer-Art-Hall'),
                (9197662::bigint, 'PHS-Foyer-HS-Hall'),
                (9197672::bigint, 'PHS-Foyer-JH-Hall'),
                (9197533::bigint, 'PHS-Foyer-Stairwell')
            ) AS x(door_id, name)
        WHERE g.name = 'PHS Foyer'
    """)
    # Default rule for the group — applies to all day types (day_type_id NULL).
    op.execute("""
        INSERT INTO door_schedule_rules (group_id, day_type_id,
            hold_open_start_offset_sec, hold_open_end_offset_sec,
            min_hold_duration_sec, max_hold_duration_sec)
        SELECT id, NULL, 0, 0, 60, 900 FROM door_schedule_groups WHERE name = 'PHS Foyer'
    """)


def downgrade() -> None:
    op.drop_table("door_schedule_rules")
    op.drop_table("door_schedule_group_members")
    op.drop_table("door_schedule_groups")
