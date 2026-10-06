"""Add per-user preferences JSONB to users.

Holds user-specific UI preferences that are too granular for system
settings but more durable than browser localStorage. First consumer:
default label printer choice for chromebook label printing (so Chance
gets the PHS printer every time without picking it again).

Stored as a flat dict, namespaced by feature key (e.g.
``{"default_label_printer_id": "phs-tech"}``). Future features add
their own keys without schema changes.

Revision ID: a097_user_preferences
Revises: a096_student_scan_progress
Create Date: 2026-05-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "a097_user_preferences"
down_revision: Union[str, None] = "a096_student_scan_progress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("preferences", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "preferences")
