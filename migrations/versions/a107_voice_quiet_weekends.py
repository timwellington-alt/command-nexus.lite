"""Voice — add quiet_weekends to voice_recipients.

When true, Sat/Sun are treated as fully quiet for this recipient.
Same override path as the existing quiet_hours_override_severity:
sufficiently-severe alerts still fire. New "alert" recipients created
from the staff-profile toggle default to quiet_weekends=TRUE so
maintenance/custodial staff don't get tickets on weekends.

Existing recipients (admin-created) default to FALSE — no behavior
change.

Revision ID: a107_voice_quiet_weekends
Revises: a106_chromebook_repair_depot
Create Date: 2026-05-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a107_voice_quiet_weekends"
down_revision: Union[str, None] = "a106_chromebook_repair_depot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "voice_recipients",
        sa.Column("quiet_weekends", sa.Boolean(),
                  nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("voice_recipients", "quiet_weekends")
