"""Per-service contract rows on vendor contracts.

A vendor may bundle multiple services, some brokered from 3rd-party
providers with independent contracts, renewal cycles, and account
numbers. Each service gets its own row so renewals + spend track
per-service rather than per-vendor.

The existing vendor-row fields (service, contract_start/end,
annual_cost, account_number, renewal_terms) stay in place for now —
kept as a legacy "primary contract" summary. A backfill inserts one
vendor_services row per vendor with non-null contract data so
history isn't lost.

Revision ID: a155_vendor_services
Revises: a154_category_budget_mapping
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a155_vendor_services"
down_revision = "a154_category_budget_mapping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_services",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vendor_id", sa.Integer,
                  sa.ForeignKey("vendor_contracts.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("service_name", sa.String(255), nullable=False),
        # Provider — the 3rd party actually delivering this service when
        # different from the vendor. Nullable — if the vendor delivers
        # it directly, leave blank.
        sa.Column("provider", sa.String(255), nullable=True),
        sa.Column("contract_start", sa.Date, nullable=True),
        sa.Column("contract_end", sa.Date, nullable=True),
        sa.Column("annual_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("account_number", sa.String(100), nullable=True),
        sa.Column("renewal_terms", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )

    # Backfill: promote each vendor's existing contract fields into a
    # matching vendor_services row so nothing is lost when the UI
    # switches to the per-service view. Skip vendors with nothing to
    # promote (no service AND no contract dates AND no cost).
    op.execute("""
        INSERT INTO vendor_services (
            vendor_id, service_name, contract_start, contract_end,
            annual_cost, account_number, renewal_terms
        )
        SELECT id,
               COALESCE(NULLIF(TRIM(service), ''), 'General service'),
               contract_start, contract_end,
               annual_cost, account_number, renewal_terms
        FROM vendor_contracts
        WHERE COALESCE(NULLIF(TRIM(service), ''), '') != ''
           OR contract_start IS NOT NULL
           OR contract_end IS NOT NULL
           OR annual_cost IS NOT NULL
           OR COALESCE(NULLIF(TRIM(account_number), ''), '') != ''
           OR COALESCE(NULLIF(TRIM(renewal_terms), ''), '') != ''
    """)


def downgrade() -> None:
    op.drop_table("vendor_services")
