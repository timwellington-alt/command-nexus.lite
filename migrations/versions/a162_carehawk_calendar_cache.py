"""CareHawk bell calendar cache — one row per event per building.

The CH1000 unit is the authority on bell timing. We cache every
event on our side so the schedule stays queryable + the door
scheduler can keep firing when the device is briefly offline (per
Tim 2026-08-17). Cache is written every time we pull the calendar
(GET) or push it (POST), and refreshed periodically by
``sync_carehawk_calendar``.

Also holds ``door_state`` — an unlock/lock tag per event that lives
in Nexus only (the CH1000 has nowhere to store it). The tag is what
the door scheduler runner reads to decide when to hold Paxton doors
open.

Identity key: ``(building_id, event_signature)`` where signature is
a stable hash of ``(template_number, start_time, end_time,
days_of_week, source)``. That composite is unique for every
real-world bell event; on the off-chance two events collide, the
CareHawk router will log a warning and treat them as distinct rows.

Revision ID: a162_carehawk_calendar_cache
Revises: a161_bell_period_door_state
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a162_carehawk_calendar_cache"
down_revision = "a161_bell_period_door_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "carehawk_calendar_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # BuildingConfig.id is an int from the settings JSON list. No FK
        # since carehawk buildings live in a settings blob, not a table.
        sa.Column("building_id", sa.Integer, nullable=False, index=True),
        # Stable identity — hash of (template_number, start_time,
        # end_time, days_of_week, source). Router computes this and
        # uses it as an upsert key.
        sa.Column("event_signature", sa.String(64), nullable=False),
        # NULL for normal-day events; 1..5 for template events.
        sa.Column("template_number", sa.SmallInteger),
        # Event fields — a compact mirror of CalendarEventDTO.
        sa.Column("type", sa.String(40), nullable=False,
                  server_default=sa.text("'ToneDistribution'")),
        sa.Column("source", sa.Integer, nullable=False),  # tone index
        sa.Column("description", sa.String(255)),
        sa.Column("distribution", sa.String(20), nullable=False,
                  server_default=sa.text("'InclusiveZones'")),
        # JSONB arrays — small, and JSONB is queryable if we ever want
        # to filter "events that fire in zone 12".
        sa.Column("destination", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("start_date", sa.String(10), nullable=False),   # YYYY-MM-DD
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("start_time", sa.String(5), nullable=False),    # HH:MM
        sa.Column("end_time", sa.String(5), nullable=False),
        sa.Column("recurrence", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("days_of_week", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        # ── Nexus-only fields ──
        # Unlock / lock tag consumed by the door scheduler. NULL =
        # neutral (no door effect during this event).
        sa.Column("door_state", sa.String(10)),
        # When we last confirmed this event exists on the device.
        # Older-than-threshold rows are candidates for the "device
        # unreachable" fallback path in the door scheduler.
        sa.Column("last_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint(
            "door_state IS NULL OR door_state IN ('unlock', 'lock')",
            name="ck_carehawk_calendar_cache_door_state",
        ),
        sa.UniqueConstraint("building_id", "event_signature",
                            name="uq_carehawk_calendar_cache_bldg_sig"),
    )
    # Door scheduler queries "give me the unlock events for building X".
    # Partial index skips the common NULL door_state (class-time events)
    # to keep this cheap.
    op.execute(
        "CREATE INDEX ix_carehawk_calendar_cache_unlock "
        "ON carehawk_calendar_cache (building_id, door_state) "
        "WHERE door_state IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_carehawk_calendar_cache_unlock")
    op.drop_table("carehawk_calendar_cache")
