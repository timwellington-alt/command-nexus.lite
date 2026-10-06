"""Add voice_recipient_id pointer on users (application-layer link, no FK
to keep the voice module decoupled per its hard boundary).

Revision ID: a047_user_voice_recipient
Revises: a046_clear_seeded_buildings
Create Date: 2026-04-16
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a047_user_voice_recipient"
down_revision: Union[str, None] = "a046_clear_seeded_buildings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("voice_recipient_id", sa.Integer, nullable=True),
    )
    # Used by /api/me/profile and the recipient-options "claimed by other user"
    # check; cheap to add now, prevents future N+1 surprise.
    op.create_index("ix_users_voice_recipient_id", "users", ["voice_recipient_id"])


def downgrade() -> None:
    op.drop_index("ix_users_voice_recipient_id", table_name="users")
    op.drop_column("users", "voice_recipient_id")
