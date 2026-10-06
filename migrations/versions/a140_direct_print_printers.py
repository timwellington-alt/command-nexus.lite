"""Add direct_print_printers table for Canon direct-to-9100 printing.

Each row is one printer profile the Print panel can send jobs to:
    - host / port for the raw TCP send
    - ppd_filename for tray-selection PostScript
    - default trays for label vs document jobs (user can override per job)

Revision ID: a140_direct_print_printers
Revises: a139_chromebook_batch_archived
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a140_direct_print_printers"
down_revision = "a139_chromebook_batch_archived"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "direct_print_printers",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer, nullable=False, server_default="9100"),
        sa.Column("ppd_filename", sa.String(255), nullable=False),
        # Tray keys are PPD InputSlot identifiers (MPT, Tray1, Tray2, …).
        sa.Column("label_default_tray", sa.String(50), nullable=False, server_default="MPT"),
        sa.Column("document_default_tray", sa.String(50), nullable=False, server_default="Tray1"),
        sa.Column("building", sa.String(50), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index(
        "ix_direct_print_printers_enabled",
        "direct_print_printers", ["enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_direct_print_printers_enabled", "direct_print_printers")
    op.drop_table("direct_print_printers")
