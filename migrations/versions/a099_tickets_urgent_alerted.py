"""Tickets module — track when an urgent-priority alert was fired.

Adds ``tickets.urgent_alerted_at`` so the urgent-escalation hook is
idempotent: ``service`` only fires push + voice when priority
transitions *into* urgent, never on subsequent updates.

Revision ID: a099_tickets_urgent_alerted
Revises: a098_tickets_module
Create Date: 2026-05-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a099_tickets_urgent_alerted"
down_revision: Union[str, None] = "a098_tickets_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("urgent_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tickets", "urgent_alerted_at")
