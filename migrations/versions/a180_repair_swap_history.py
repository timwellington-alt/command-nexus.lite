"""Add swap_history JSONB to ticket_chromebook_repair.

Powers the "Swap in replacement" flow on the repair-depot detail page:
when a device is unrepairable, a tech scans a spare's serial/asset tag,
Nexus atomically transfers the asset tag + cart OU from broken → replacement
via the Google Chrome OS API and appends a record here so the ticket
tells the full story of what physically happened.

Each row in the JSONB array captures:
    {
      "at": ISO8601,
      "actor": <email>,
      "correlation_id": <uuid>,
      "old": {"serial": ..., "asset_tag": ..., "model": ..., "cart_ou": ...},
      "new": {"serial": ..., "model": ..., "prior_ou": ...},
      "target_repair_ou": <where the broken device landed>,
      "steps": [{step, status, error?}]
    }

Revision ID: a180_repair_swap_history
Revises: a179_student_reconcile
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a180_repair_swap_history"
down_revision = "a179_student_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ticket_chromebook_repair",
        sa.Column("swap_history", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ticket_chromebook_repair", "swap_history")
