"""Scope vendor attachments to an optional service row.

Some vendors (Insight is the canonical example) resell licensing +
hardware from multiple upstream providers under one master contract.
Attachments need to sit alongside the specific service they document
(Google Workspace for Ed renewal, Chromebook parts invoice, HVAC
service report) so the operator can find them by service, not just
by vendor.

Making it nullable — existing vendor-level attachments (insurance
certs, SOC 2 reports, master MOU) stay attached to the vendor row
without a specific service.

Revision ID: a156_service_scoped_attachments
Revises: a155_vendor_services
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a156_service_scoped_attachments"
down_revision = "a155_vendor_services"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vendor_attachments",
        sa.Column("service_id", sa.Integer,
                  sa.ForeignKey("vendor_services.id", ondelete="SET NULL"),
                  nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_column("vendor_attachments", "service_id")
