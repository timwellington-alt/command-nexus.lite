"""Chromebook OU-history table — cart/OU changes over time.

Records every OU delta observed by `sync_chromebook_cache` so the
device-history page can render a cart-move timeline. Empty for
pre-2026-07-23 history — audit-log mining for older moves is out of
scope; we start tracking going forward.

Key choice: log the delta only when `to_ou != from_ou`. This keeps the
row count small (only 50-200 rows/year expected) and every row is a
real move rather than noise from an unchanged OU on every sync.

`google_device_id` is the FK anchor (Chromebook Directory API device
id) — stable across renames and re-enrollments. `serial` and
`asset_tag` are denormalized so operator queries by serial don't need
a join.

Revision ID: a148_chromebook_ou_history
Revises: a147_ticket_on_behalf_of
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a148_chromebook_ou_history"
down_revision = "a147_ticket_on_behalf_of"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chromebook_ou_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("google_device_id", sa.String(100), nullable=False, index=True),
        sa.Column("serial", sa.String(100), nullable=False, index=True),
        sa.Column("asset_tag", sa.String(100)),
        sa.Column("from_ou", sa.Text),  # NULL for the first-observed entry
        sa.Column("to_ou", sa.Text, nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("source", sa.String(30), nullable=False, server_default="sync"),
    )
    op.create_index(
        "ix_chromebook_ou_history_serial_detected",
        "chromebook_ou_history",
        ["serial", "detected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chromebook_ou_history_serial_detected", "chromebook_ou_history")
    op.drop_table("chromebook_ou_history")
