"""Facility module — add reservable + capacity to rooms.

Prerequisite for the upcoming Schedule / Reservations module: each room
gets a `reservable` flag (default false) and an optional `capacity`.
The schedule module will reject booking attempts against rooms with
`reservable=false`, and use capacity for attendee-count validation.

Revision ID: a100_facility_rooms_reservable
Revises: a099_tickets_urgent_alerted
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a100_facility_rooms_reservable"
down_revision: Union[str, None] = "a099_tickets_urgent_alerted"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facility_rooms",
        sa.Column("reservable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "facility_rooms",
        sa.Column("capacity", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("facility_rooms", "capacity")
    op.drop_column("facility_rooms", "reservable")
