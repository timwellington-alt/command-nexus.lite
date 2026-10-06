"""Ticket 'on behalf of' — track a second party the ticket is *for*.

Adds two columns to `tickets`:
  • on_behalf_of_user_id — FK to users.id, ON DELETE SET NULL. Resolved
    from the email at create time when possible. NULL when the ticket
    is self-submitted (the default) or when the email doesn't match
    any user (external partner / stale account).
  • on_behalf_of_email — the raw email as entered. Kept even after user
    resolution so unmatched addresses (external teacher, retired staff)
    are still displayed on the ticket.

`requester_user_id` stays as the actual submitter — the audit trail
tells the truth about who filed the ticket. Downstream notifications
route to `on_behalf_of_user_id` when set, otherwise to the requester.

Revision ID: a147_ticket_on_behalf_of
Revises: a146_inventory_compat_models
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a147_ticket_on_behalf_of"
down_revision = "a146_inventory_compat_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("on_behalf_of_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("on_behalf_of_email", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_tickets_on_behalf_of_user_id",
        "tickets", ["on_behalf_of_user_id"],
        postgresql_where=sa.text("on_behalf_of_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_tickets_on_behalf_of_user_id", "tickets")
    op.drop_column("tickets", "on_behalf_of_email")
    op.drop_column("tickets", "on_behalf_of_user_id")
