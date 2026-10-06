"""Per-recipient voice/push channel toggles.

Lets admins disable a single channel per recipient (e.g. push-only,
voice-only) without having to delete the phone number or unsubscribe
the browser. Both default true so existing behavior is preserved.

Revision ID: a055_recipient_channel_toggles
Revises: a054_cast_devices_alert_routing
Create Date: 2026-04-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a055_recipient_channel_toggles"
down_revision: Union[str, None] = "a054_cast_devices_alert_routing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "voice_recipients",
        sa.Column("voice_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "voice_recipients",
        sa.Column("push_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("voice_recipients", "push_enabled")
    op.drop_column("voice_recipients", "voice_enabled")
