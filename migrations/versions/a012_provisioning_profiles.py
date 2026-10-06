"""Provisioning profiles — configurable building+role → group/OU/access mappings.

Revision ID: a012_provisioning_profiles
Revises: a011_staff_directory
Create Date: 2026-03-29
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a012_provisioning_profiles"
down_revision: Union[str, None] = "a011_staff_directory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provisioning_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("building", sa.String(50), nullable=False),
        sa.Column("role_type", sa.String(50), nullable=False),
        sa.Column("google_ou", sa.String(500), nullable=True),
        sa.Column("google_groups", sa.Text(), nullable=True),
        sa.Column("ad_ou", sa.String(500), nullable=True),
        sa.Column("ad_groups", sa.Text(), nullable=True),
        sa.Column("paxton_access_level", sa.String(255), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("building", "role_type", name="uq_profile_building_role"),
    )
    op.create_index("ix_provisioning_profiles_building", "provisioning_profiles", ["building"])


def downgrade() -> None:
    op.drop_table("provisioning_profiles")
