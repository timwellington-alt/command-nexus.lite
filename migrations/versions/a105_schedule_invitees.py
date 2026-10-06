"""Tickets module — schedule invitees list.

Adds ``ticket_schedule.invitees`` as a JSON-text list of lowercased
emails. When an event is marked ``private``, the calendar/occurrences
API masks title + sub_category + requester for viewers *not* in:

  - tickets.admin
  - tickets.schedule.approve
  - requester_user_id / assignee_user_id
  - the new ``invitees`` list

Storing emails (not user IDs) keeps the round-trip simple: the form
sources via staff-search, saves the chosen emails verbatim, and the
visibility check is a plain ``user.email.lower() in invitees``.

Revision ID: a105_schedule_invitees
Revises: a104_users_out_of_office
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a105_schedule_invitees"
down_revision: Union[str, None] = "a104_users_out_of_office"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ticket_schedule",
        sa.Column("invitees", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("ticket_schedule", "invitees")
