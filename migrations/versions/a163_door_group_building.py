"""Link door_schedule_groups to a CareHawk building.

Post-consolidation (2026-08-17): the door scheduler reads its bell
data from carehawk_calendar_cache, which is per-building. Each door
group must therefore identify which building's bell schedule to
follow. Nullable for now — the runner skips groups without a
building set, and the admin UI prompts to pick one when creating.

Backfill: no default because BuildingConfig.id is a district-specific
JSON key; setting it here would be a district-specific value baked
into a migration. Existing PHS Foyer group operator will pick from
the UI on first edit.

Revision ID: a163_door_group_building
Revises: a162_carehawk_calendar_cache
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a163_door_group_building"
down_revision = "a162_carehawk_calendar_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "door_schedule_groups",
        sa.Column("carehawk_building_id", sa.Integer, nullable=True),
    )
    # Index — the runner queries by (enabled + building) to fan out.
    op.create_index(
        "ix_door_schedule_groups_carehawk_building",
        "door_schedule_groups", ["carehawk_building_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_door_schedule_groups_carehawk_building",
                  table_name="door_schedule_groups")
    op.drop_column("door_schedule_groups", "carehawk_building_id")
