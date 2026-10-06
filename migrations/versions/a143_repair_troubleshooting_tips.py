"""Add AI-generated troubleshooting tips to chromebook repair tickets.

Triggered post-intake — reported_issue + failure_categories get sent to
Qwen (Tim's desktop) and the returned tips are stored on the depot side
row. Re-runs only when the input hash changes (so intake edits refresh
tips, but idle viewing of the ticket doesn't burn calls).

Revision ID: a143_repair_troubleshooting_tips
Revises: a142_batch_ou_fields
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a143_repair_troubleshooting_tips"
down_revision = "a142_batch_ou_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket_chromebook_repair",
                  sa.Column("troubleshooting_tips", sa.Text, nullable=True))
    op.add_column("ticket_chromebook_repair",
                  sa.Column("troubleshooting_tips_at",
                            sa.DateTime(timezone=True), nullable=True))
    # Hash of (reported_issue + sorted failure_categories + model). Lets the
    # job skip regen when the operator opens the ticket without changing
    # anything, and re-fire when they add a new symptom.
    op.add_column("ticket_chromebook_repair",
                  sa.Column("troubleshooting_tips_input_hash",
                            sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket_chromebook_repair", "troubleshooting_tips_input_hash")
    op.drop_column("ticket_chromebook_repair", "troubleshooting_tips_at")
    op.drop_column("ticket_chromebook_repair", "troubleshooting_tips")
