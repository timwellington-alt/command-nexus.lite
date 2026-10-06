"""Add unique constraint on doors.name.

Revision ID: a008_door_name_unique
Revises: a007_teacher_contacts
Create Date: 2026-03-28
"""

from typing import Sequence, Union
from alembic import op

revision: str = "a008_door_name_unique"
down_revision: Union[str, None] = "a007_teacher_contacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint("uq_doors_name", "doors", ["name"])


def downgrade() -> None:
    op.drop_constraint("uq_doors_name", "doors", type_="unique")
