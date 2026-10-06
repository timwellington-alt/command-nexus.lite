"""Persist Target OU / Orphan OU / Sweep / Code type on saved batches.

The batch page has these controls but Save previously dropped them —
loading a saved batch then had to have the operator re-enter every OU
field. Adding four nullable columns closes that gap. All are optional
so existing rows keep working.

Revision ID: a142_batch_ou_fields
Revises: a141_chromebook_cart_alerts
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a142_batch_ou_fields"
down_revision = "a141_chromebook_cart_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chromebook_batches",
                  sa.Column("target_ou", sa.Text, nullable=True))
    op.add_column("chromebook_batches",
                  sa.Column("orphan_ou", sa.Text, nullable=True))
    op.add_column("chromebook_batches",
                  sa.Column("sweep_orphans", sa.Boolean, nullable=False,
                            server_default=sa.text("false")))
    op.add_column("chromebook_batches",
                  sa.Column("code_type", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("chromebook_batches", "code_type")
    op.drop_column("chromebook_batches", "sweep_orphans")
    op.drop_column("chromebook_batches", "orphan_ou")
    op.drop_column("chromebook_batches", "target_ou")
