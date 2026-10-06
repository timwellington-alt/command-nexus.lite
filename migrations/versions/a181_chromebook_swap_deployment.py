"""Add swap_deployment JSONB to chromebook_cache.

Purpose: when a device is physically swapped via the depot flow, both
the replacement and the retired hardware get a durable per-device
marker so anyone looking up the device in Nexus (or scanning its asset
tag) immediately sees the swap outcome — without having to trace back
through the depot ticket's swap_history.

Shape (most recent event only — overwrites on subsequent swaps):
    {
      "role": "replacement" | "retired",
      "at": ISO8601,
      "actor": <email>,
      "ticket_id": <int>,
      "counterpart_serial": <serial>,
      "correlation_id": <uuid>,
      "asset_tag": <tag transferred>
    }

Revision ID: a181_chromebook_swap_deployment
Revises: a180_repair_swap_history
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a181_chromebook_swap_deployment"
down_revision = "a180_repair_swap_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chromebook_cache",
        sa.Column("swap_deployment", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chromebook_cache", "swap_deployment")
