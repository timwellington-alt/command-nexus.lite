"""Add room_roster_cache table for Google Sheets room roster sync.

Captures the latest snapshot of the per-building room roster Google
Sheets that drive the roster module's room assignments. The
``room_roster_cache_job`` rewrites this table every sync cycle.

Uses ``IF NOT EXISTS`` so running this against the production DB
(where the table was created ad-hoc) is a no-op.

Revision ID: a039_room_roster_cache
Revises: a038_ops_email_export
Create Date: 2026-04-11
"""

from typing import Sequence, Union
from alembic import op

revision: str = "a039_room_roster_cache"
down_revision: Union[str, None] = "a038_ops_email_export"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS room_roster_cache (
            id SERIAL PRIMARY KEY,
            building VARCHAR(20) NOT NULL,
            room VARCHAR(20) NOT NULL,
            name VARCHAR(255) NOT NULL,
            assignment VARCHAR(255),
            floor VARCHAR(50),
            is_esc BOOLEAN DEFAULT FALSE,
            cached_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_room_roster_cache_building ON room_roster_cache (building)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_room_roster_cache_name ON room_roster_cache (lower(name))")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS room_roster_cache")
