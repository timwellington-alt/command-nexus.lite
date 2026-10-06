"""Add Paxton department fields to provisioning profiles.

Revision ID: a030_paxton_dept
Revises: a029_print_status
Create Date: 2026-04-08
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a030_paxton_dept"
down_revision: Union[str, None] = "a029_print_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "provisioning_profiles",
        sa.Column("paxton_department_id", sa.Integer()),
    )
    op.add_column(
        "provisioning_profiles",
        sa.Column("paxton_department_name", sa.String(255)),
    )


def downgrade() -> None:
    op.drop_column("provisioning_profiles", "paxton_department_name")
    op.drop_column("provisioning_profiles", "paxton_department_id")
