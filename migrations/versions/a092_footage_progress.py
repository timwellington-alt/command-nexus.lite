"""Add progress_message to bus_footage_requests.

Operators want visibility while a request is in-flight ("authenticating...",
"pulling channel 3/6", "writing HD5"). Service updates this column at each
step so the existing 30s UI poll surfaces live progress without us having
to add a websocket layer.

Revision ID: a092_footage_progress
Revises: a091_bus_footage
Create Date: 2026-05-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a092_footage_progress"
down_revision: Union[str, None] = "a091_bus_footage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bus_footage_requests",
        sa.Column("progress_message", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bus_footage_requests", "progress_message")
