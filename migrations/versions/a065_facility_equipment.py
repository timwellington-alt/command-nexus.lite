"""Facility (fixed) equipment — schema only.

Adds a third item_type ('fixed') for building-resident equipment that
isn't checked out and isn't disposed in the same way as movable assets.
Per-item documents (manuals, service docs, warranty PDFs), service log
entries, and replacement-parts catalog all live in their own tables.

Phase-1 migration: lays the schema for phases 1-4 in a single shot so we
don't ship four separate migrations across the rollout.

Revision ID: a065_facility_equipment
Revises: a064_seed_network_manage
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a065_facility_equipment"
down_revision: Union[str, None] = "a064_seed_network_manage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extend inventory_items ─────────────────────────────────────────
    op.add_column("inventory_items", sa.Column("install_date", sa.Date()))
    op.add_column("inventory_items", sa.Column("service_interval_days", sa.Integer()))
    op.add_column("inventory_items", sa.Column("next_pm_due_date", sa.Date()))
    op.add_column("inventory_items", sa.Column("warranty_provider", sa.String(255)))
    op.add_column("inventory_items", sa.Column("warranty_starts", sa.Date()))
    op.add_column("inventory_items", sa.Column("warranty_policy_number", sa.String(100)))
    op.add_column("inventory_items", sa.Column("warranty_contact", sa.String(255)))
    op.add_column("inventory_items", sa.Column("decommissioned_at", sa.DateTime(timezone=True)))
    op.add_column("inventory_items", sa.Column("decommissioned_reason", sa.Text()))
    op.add_column(
        "inventory_items",
        sa.Column(
            "replaced_by_item_id",
            sa.Integer(),
            sa.ForeignKey("inventory_items.id", ondelete="SET NULL"),
        ),
    )

    # Allow item_type='fixed', status='decommissioned', and the new
    # 'email_intake_fixed' provenance value. Drop-and-recreate the
    # CHECK constraints since Postgres has no ALTER CHECK.
    op.drop_constraint("ck_inventory_items_item_type", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_item_type",
        "inventory_items",
        "item_type IN ('asset', 'stock', 'fixed')",
    )
    op.drop_constraint("ck_inventory_items_status", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_status",
        "inventory_items",
        "status IN ('in_service', 'in_storage', 'loaned', 'in_repair', 'disposal_pending', 'disposed', 'decommissioned')",
    )
    op.drop_constraint("ck_inventory_items_created_via", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_created_via",
        "inventory_items",
        "created_via IN ('manual', 'scan_commission', 'order_receipt', 'bulk_import', 'email_intake', 'email_intake_fixed', 'libre_import')",
    )
    op.create_index(
        "ix_inventory_items_pm_due", "inventory_items",
        ["next_pm_due_date"],
        postgresql_where=sa.text("next_pm_due_date IS NOT NULL AND decommissioned_at IS NULL"),
    )
    op.create_index(
        "ix_inventory_items_decommissioned", "inventory_items", ["decommissioned_at"],
        postgresql_where=sa.text("decommissioned_at IS NOT NULL"),
    )

    # ── Documents (manuals, service docs, warranty PDFs) ──────────────
    op.create_table(
        "inventory_item_documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(),
                  sa.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("doc_kind", sa.String(20), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("file_mime", sa.String(50), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False, index=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(255)),
        sa.Column("title", sa.String(255)),
        sa.Column("notes", sa.Text()),
        sa.Column("captured_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("captured_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "doc_kind IN ('manual', 'service_doc', 'warranty_doc', 'other')",
            name="ck_inventory_item_documents_kind",
        ),
    )

    # ── Service log ───────────────────────────────────────────────────
    op.create_table(
        "inventory_service_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(),
                  sa.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("performed_at", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("performed_by_text", sa.String(255)),
        sa.Column("cost_cents", sa.Integer()),
        sa.Column("notes", sa.Text()),
        sa.Column("doc_id", sa.Integer(),
                  sa.ForeignKey("inventory_item_documents.id", ondelete="SET NULL")),
        sa.Column("created_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_inventory_service_log_item_perf",
        "inventory_service_log", ["item_id", "performed_at"],
    )

    # ── Replacement parts catalog ─────────────────────────────────────
    op.create_table(
        "inventory_replacement_parts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(),
                  sa.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("part_name", sa.String(255), nullable=False),
        sa.Column("part_number", sa.String(100)),
        sa.Column("supplier_name", sa.String(255)),
        sa.Column("supplier_url", sa.Text()),
        sa.Column("price_cents", sa.Integer()),
        sa.Column("last_priced_at", sa.Date()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("inventory_replacement_parts")
    op.drop_index("ix_inventory_service_log_item_perf", table_name="inventory_service_log")
    op.drop_table("inventory_service_log")
    op.drop_table("inventory_item_documents")

    op.drop_index("ix_inventory_items_decommissioned", table_name="inventory_items")
    op.drop_index("ix_inventory_items_pm_due", table_name="inventory_items")

    op.drop_constraint("ck_inventory_items_created_via", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_created_via", "inventory_items",
        "created_via IN ('manual', 'scan_commission', 'order_receipt', 'bulk_import', 'email_intake', 'libre_import')",
    )
    op.drop_constraint("ck_inventory_items_status", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_status", "inventory_items",
        "status IN ('in_service', 'in_storage', 'loaned', 'in_repair', 'disposal_pending', 'disposed')",
    )
    op.drop_constraint("ck_inventory_items_item_type", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_item_type", "inventory_items",
        "item_type IN ('asset', 'stock')",
    )

    for col in (
        "replaced_by_item_id", "decommissioned_reason", "decommissioned_at",
        "warranty_contact", "warranty_policy_number", "warranty_starts",
        "warranty_provider", "next_pm_due_date", "service_interval_days",
        "install_date",
    ):
        op.drop_column("inventory_items", col)
