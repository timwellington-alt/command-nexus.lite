"""Users — out-of-office until date.

Backs the routing-rule backup chain: when a rule's primary assignee
has a non-NULL ``out_of_office_until`` >= today, the resolver walks
to the next entry in ``assignee_emails`` (Phase 3). Auto-expires
without a cron — every routing decision compares to today's date.

Revision ID: a104_users_out_of_office
Revises: a103_drop_schedule_approver
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a104_users_out_of_office"
down_revision: Union[str, None] = "a103_drop_schedule_approver"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("out_of_office_until", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "out_of_office_until")
