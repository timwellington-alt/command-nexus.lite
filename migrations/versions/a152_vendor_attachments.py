"""Attachments on vendor contract rows.

Every vendor now needs supporting docs on file: the completed HB 96
data-sharing questionnaire, signed MOUs, SOC 2 reports, insurance
certs, invoices. Storing them on disk under
``/app/data/vendor_docs/{vendor_id}/`` with metadata in this table.

Revision ID: a152_vendor_attachments
Revises: a151_chromebook_aue_date
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a152_vendor_attachments"
down_revision = "a151_chromebook_aue_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_attachments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vendor_id", sa.Integer,
                  sa.ForeignKey("vendor_contracts.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("stored_path", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(100)),
        sa.Column("size_bytes", sa.Integer),
        sa.Column("description", sa.Text),
        sa.Column("uploaded_by", sa.String(255)),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("vendor_attachments")
