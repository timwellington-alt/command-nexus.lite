"""Add index on users.voice_recipient_id (was forgotten in a047 — used by
/api/me/profile and the 'claimed by other user' lookup).

Revision ID: a048_index_user_voice_recipient
Revises: a047_user_voice_recipient
Create Date: 2026-04-16
"""

from typing import Sequence, Union
from alembic import op

revision: str = "a048_index_user_voice_recipient"
down_revision: Union[str, None] = "a047_user_voice_recipient"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_users_voice_recipient_id",
        "users",
        ["voice_recipient_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_users_voice_recipient_id", table_name="users", if_exists=True)
