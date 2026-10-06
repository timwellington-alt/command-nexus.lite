"""Seed provisioning profiles (stub).

The reference deployment seeded building+role → OU/groups mappings
here. For the template, this migration is a no-op; populate your
district's mappings via the Settings UI after first boot.

Revision ID: a018_seed_provisioning_profiles
Revises: a017_roster_permissions
"""
from typing import Sequence, Union
from alembic import op  # noqa: F401

revision: str = "a018_seed_provisioning_profiles"
down_revision: str = "a017_roster_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
