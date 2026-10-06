"""Tickets module — schedule recurrence fields.

Phase 2 adds a simple recurrence model: one schedule ticket can repeat
daily / weekly / monthly until an end date. Weekly uses ``repeat_days``
(JSON list of weekday integers, 0=Mon .. 6=Sun) for partial weeks like
"every Mon/Wed/Fri."

Deliberately *not* RFC-5545 RRULE — schools don't need BYHOUR/BYSETPOS/
exceptions. If the operational complexity ever justifies it, swap in
dateutil.rrule and migrate.

On approval, ``service.approve_schedule`` expands the recurrence into
individual occurrences and spawns one child work ticket per occurrence
per service (custodial/maintenance/technology).

Revision ID: a102_schedule_recurrence
Revises: a101_tickets_schedule_category
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a102_schedule_recurrence"
down_revision: Union[str, None] = "a101_tickets_schedule_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ticket_schedule",
        sa.Column("repeat_freq", sa.String(20), nullable=True),
    )
    op.add_column(
        "ticket_schedule",
        sa.Column("repeat_until", sa.Date(), nullable=True),
    )
    op.add_column(
        "ticket_schedule",
        sa.Column("repeat_days", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ticket_schedule_repeat_freq_check", "ticket_schedule",
        "repeat_freq IS NULL OR repeat_freq IN ('daily','weekly','monthly')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ticket_schedule_repeat_freq_check", "ticket_schedule", type_="check",
    )
    op.drop_column("ticket_schedule", "repeat_days")
    op.drop_column("ticket_schedule", "repeat_until")
    op.drop_column("ticket_schedule", "repeat_freq")
