"""Access control tables — doors and door events.

Revision ID: a005_access
Revises: a004_staff
Create Date: 2026-03-27
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a005_access"
down_revision: Union[str, None] = "a004_staff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "doors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("paxton_door_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("building", sa.String(50), nullable=True),
        sa.Column("group_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paxton_door_id"),
    )
    op.create_index("ix_doors_paxton_door_id", "doors", ["paxton_door_id"])
    op.create_index("ix_doors_building", "doors", ["building"])

    op.create_table(
        "door_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("paxton_event_id", sa.Integer(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.Integer(), nullable=False),
        sa.Column("event_description", sa.String(100), nullable=True),
        sa.Column("door_name", sa.String(255), nullable=True),
        sa.Column("door_group_id", sa.Integer(), nullable=True),
        sa.Column("peripheral_id", sa.Integer(), nullable=True),
        sa.Column("person_name", sa.String(255), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("card_no", sa.String(50), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paxton_event_id"),
    )
    op.create_index("ix_door_events_paxton_event_id", "door_events", ["paxton_event_id"])
    op.create_index("ix_door_events_event_time", "door_events", ["event_time"])
    op.create_index("ix_door_events_door_name", "door_events", ["door_name"])
    op.create_index("ix_door_events_person_name", "door_events", ["person_name"])
    op.create_index("ix_door_events_user_id", "door_events", ["user_id"])


def downgrade() -> None:
    op.drop_table("door_events")
    op.drop_table("doors")
