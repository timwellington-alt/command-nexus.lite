"""Remove hardcoded buildings seeded in a044.

Buildings are configured through the Settings UI, not seeded in migrations.

Revision ID: a046_clear_seeded_buildings
Revises: a045_consolidate_tech_routing
Create Date: 2026-04-16
"""

from typing import Sequence, Union
from alembic import op

revision: str = "a046_clear_seeded_buildings"
down_revision: Union[str, None] = "a045_consolidate_tech_routing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM alert_buildings")


def downgrade() -> None:
    pass  # Data cannot be restored; reconfigure through Settings UI
