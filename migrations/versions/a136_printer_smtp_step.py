"""Add SMTP-configured step to the printer migration tracker.

Tim's configuring SMTP scan-to-email manually on each Canon via Remote UI
(no clean DSI sub-category for SMTP-only on this firmware, no programmatic
write surface). The checkbox lets him mark progress as he walks the fleet.

Revision ID: a136_printer_smtp_step
Revises: a135_projector_cache
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a136_printer_smtp_step"
down_revision = "a135_projector_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "printer_migration_status",
        sa.Column("step_smtp_configured", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("printer_migration_status", "step_smtp_configured")
