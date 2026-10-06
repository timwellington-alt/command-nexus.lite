"""Per-event ``silence_at_device`` flag. When true, the event is still
written to the CH1000 (so timing / next-bell lookups work) but with an
empty Destination — the bell fires silently. Used to dedupe co-timed
bells across grades that would otherwise stack rings on the same
speakers.

Revision ID: a178_event_silence_at_device
Revises: a177_roster_email_overrides
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a178_event_silence_at_device"
down_revision = "a177_roster_email_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "carehawk_calendar_cache",
        sa.Column("silence_at_device", sa.Boolean,
                  nullable=False, server_default=sa.text("FALSE")),
    )


def downgrade() -> None:
    op.drop_column("carehawk_calendar_cache", "silence_at_device")
