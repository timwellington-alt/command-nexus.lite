"""Inventory module — 10 tables + indexes + check constraints + permissions.

Implements the v2 spec at Project Specs/inventory/INVENTORY_MODULE_IMPLEMENTATION_v2.md.

Notable departures from the spec:
- INT primary keys throughout (Nexus convention) instead of UUIDs.
- Checkout target is captured as snapshot email + name rather than a hard FK
  to a staff table — staff turn over and we want the audit trail to survive.
- alert_routes uses a JSON array column for recipients (text[] would force
  Postgres-only; we already use jsonb elsewhere for tag-list settings).

Revision ID: a050_inventory_module
Revises: a049_user_dashboard_layout
Create Date: 2026-04-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a050_inventory_module"
down_revision: Union[str, None] = "a049_user_dashboard_layout"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Categories ────────────────────────────────────────────────────────
    op.create_table(
        "inventory_categories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("icon", sa.String(50)),
        sa.Column("default_is_loanable", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("default_is_capitalized", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("sort_order", sa.Integer, server_default="100", nullable=False),
        sa.Column("archived", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Pending orders (parsed from email) ────────────────────────────────
    op.create_table(
        "inventory_pending_orders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("po_number", sa.String(100), nullable=False, index=True),
        sa.Column("vendor", sa.String(50), nullable=False),
        sa.Column("parser_used", sa.String(50), nullable=False),
        sa.Column("order_date", sa.Date),
        sa.Column("expected_ship_date", sa.Date),
        sa.Column("total_cents", sa.Integer),
        sa.Column("status", sa.String(20), nullable=False, server_default="parsed", index=True),
        sa.Column("source_email_message_id", sa.Text, nullable=False),
        sa.Column("source_email_subject", sa.Text),
        sa.Column("forwarded_by_email", sa.String(255), nullable=False),
        sa.Column("forwarded_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("raw_email_body", sa.Text, nullable=False),
        sa.Column("parse_errors", postgresql.JSONB),
        sa.Column("received_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_email_message_id", name="uq_pending_orders_msg_id"),
    )

    # ── Pending order line items ──────────────────────────────────────────
    op.create_table(
        "inventory_pending_order_lines",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("pending_order_id", sa.Integer, sa.ForeignKey("inventory_pending_orders.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("manufacturer", sa.String(100)),
        sa.Column("model", sa.String(255)),
        sa.Column("vendor_sku", sa.String(100)),
        # expected_serials is a JSONB array — Postgres text[] would lock us in further
        sa.Column("expected_serials", postgresql.JSONB),
        sa.Column("ordered_qty", sa.Integer, nullable=False, server_default="1"),
        sa.Column("received_qty", sa.Integer, nullable=False, server_default="0"),
        sa.Column("unit_price_cents", sa.Integer),
        sa.Column("suggested_category_id", sa.Integer, sa.ForeignKey("inventory_categories.id", ondelete="SET NULL")),
        sa.Column("line_number", sa.Integer, nullable=False, server_default="1"),
    )

    # ── Items (assets + stock containers) ─────────────────────────────────
    op.create_table(
        "inventory_items",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("item_type", sa.String(10), nullable=False),  # 'asset' or 'stock'
        sa.Column("serial_number", sa.String(255), nullable=False),
        sa.Column("manufacturer", sa.String(100)),
        sa.Column("model", sa.String(255)),
        sa.Column("asset_tag", sa.String(50)),
        sa.Column("is_capitalized", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("inventory_categories.id", ondelete="SET NULL"), index=True),
        sa.Column("description", sa.Text),
        sa.Column("building_code", sa.String(20), index=True),
        sa.Column("room", sa.String(100)),
        sa.Column("status", sa.String(30), nullable=False, server_default="in_storage", index=True),
        sa.Column("condition", sa.String(20), nullable=False, server_default="good"),
        sa.Column("is_loanable", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
        sa.Column("reorder_threshold", sa.Integer),
        sa.Column("purchase_date", sa.Date),
        sa.Column("purchase_price_cents", sa.Integer),
        sa.Column("po_number", sa.String(100)),
        sa.Column("pending_order_line_id", sa.Integer, sa.ForeignKey("inventory_pending_order_lines.id", ondelete="SET NULL"), index=True),
        sa.Column("warranty_expires", sa.Date),
        sa.Column("notes", sa.Text),
        sa.Column("department", sa.String(50), nullable=False, server_default="tech"),
        sa.Column("created_via", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("item_type IN ('asset', 'stock')", name="ck_inventory_items_item_type"),
        sa.CheckConstraint("status IN ('in_service', 'in_storage', 'loaned', 'in_repair', 'disposal_pending', 'disposed')", name="ck_inventory_items_status"),
        sa.CheckConstraint("condition IN ('new', 'good', 'fair', 'poor', 'failed')", name="ck_inventory_items_condition"),
        sa.CheckConstraint("created_via IN ('manual', 'scan_commission', 'order_receipt', 'bulk_import')", name="ck_inventory_items_created_via"),
        sa.CheckConstraint("quantity >= 0", name="ck_inventory_items_qty_nonneg"),
    )
    # UNIQUE on (manufacturer, serial_number, item_type) WHERE deleted_at IS NULL
    op.create_index(
        "uq_inventory_items_serial",
        "inventory_items",
        ["manufacturer", "serial_number", "item_type"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_inventory_items_asset_tag",
        "inventory_items",
        ["asset_tag"],
        unique=True,
        postgresql_where=sa.text("asset_tag IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index("ix_inventory_items_building_room", "inventory_items", ["building_code", "room"])
    op.create_index("ix_inventory_items_reorder", "inventory_items", ["item_type", "quantity"])

    # ── Location history ──────────────────────────────────────────────────
    op.create_table(
        "inventory_location_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("from_building", sa.String(20)),
        sa.Column("from_room", sa.String(100)),
        sa.Column("to_building", sa.String(20)),
        sa.Column("to_room", sa.String(100)),
        sa.Column("moved_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reason", sa.Text),
        sa.Column("moved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Checkouts (loan ledger) ───────────────────────────────────────────
    op.create_table(
        "inventory_checkouts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False, index=True),
        # Snapshot rather than FK — staff churn shouldn't break the ledger
        sa.Column("checked_out_to_email", sa.String(255), nullable=False),
        sa.Column("checked_out_to_name", sa.String(255)),
        sa.Column("checked_out_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=False),
        sa.Column("checked_out_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expected_return_at", sa.DateTime(timezone=True)),
        sa.Column("returned_at", sa.DateTime(timezone=True)),
        sa.Column("returned_to_building", sa.String(20)),
        sa.Column("returned_to_room", sa.String(100)),
        sa.Column("notes", sa.Text),
    )
    op.create_index(
        "ix_inventory_checkouts_open",
        "inventory_checkouts",
        ["item_id"],
        postgresql_where=sa.text("returned_at IS NULL"),
    )

    # ── Audit sessions (cycle counts) ─────────────────────────────────────
    op.create_table(
        "inventory_audit_sessions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("room", sa.String(100)),
        sa.Column("started_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text),
    )

    op.create_table(
        "inventory_audit_scans",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer, sa.ForeignKey("inventory_audit_sessions.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("inventory_items.id", ondelete="SET NULL")),
        sa.Column("raw_value", sa.String(500), nullable=False),
        # 'expected', 'misplaced', 'unknown', 'disposed_resurfaced'
        sa.Column("classification", sa.String(30), nullable=False),
        sa.Column("scanner_source", sa.String(20)),
        sa.Column("scanned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Enrichment cache ──────────────────────────────────────────────────
    op.create_table(
        "inventory_enrichment_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("serial_lookup", sa.String(255), nullable=False, index=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("result", postgresql.JSONB),
        sa.Column("error", sa.Text),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("serial_lookup", "provider", name="uq_enrichment_cache_lookup_provider"),
    )

    # ── Shipping docs ─────────────────────────────────────────────────────
    op.create_table(
        "inventory_shipping_docs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("pending_order_id", sa.Integer, sa.ForeignKey("inventory_pending_orders.id", ondelete="SET NULL"), index=True),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("file_mime", sa.String(50), nullable=False),
        sa.Column("file_size_bytes", sa.Integer, nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False, index=True),
        sa.Column("captured_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("forwarded_to_treasurer_at", sa.DateTime(timezone=True)),
        sa.Column("treasurer_message_id", sa.Text),
        sa.Column("notes", sa.Text),
    )

    # ── Disposals ─────────────────────────────────────────────────────────
    op.create_table(
        "inventory_disposals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("method", sa.String(30), nullable=False),
        sa.Column("disposed_at", sa.Date, nullable=False),
        sa.Column("disposed_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=False),
        sa.Column("recipient_name", sa.String(255)),
        sa.Column("recipient_contact", sa.String(255)),
        sa.Column("data_wiped", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("wipe_method", sa.String(100)),
        sa.Column("wiped_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("wipe_verified_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("wipe_verified_at", sa.DateTime(timezone=True)),
        sa.Column("certificate_file_path", sa.Text),
        sa.Column("sale_price_cents", sa.Integer),
        sa.Column("notes", sa.Text),
        sa.Column("bulk_session", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.Column("reversed_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reversal_reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # Spec §2.11 constraints — enforced at DB layer
        sa.CheckConstraint(
            "(data_wiped = false) OR (wiped_by_user_id IS NOT NULL AND wipe_method IS NOT NULL)",
            name="ck_disposal_wipe_requires_wiper",
        ),
        sa.CheckConstraint(
            "(wipe_verified_at IS NULL) OR (wipe_verified_by_user_id IS NOT NULL AND wipe_verified_by_user_id != wiped_by_user_id)",
            name="ck_disposal_verifier_distinct",
        ),
        sa.CheckConstraint(
            "method IN ('e_waste', 'surplus_auction', 'donation', 'destroyed', 'lost', 'stolen', 'returned_to_vendor', 'trade_in')",
            name="ck_disposal_method",
        ),
    )

    op.create_table(
        "inventory_disposal_items",
        sa.Column("disposal_id", sa.Integer, sa.ForeignKey("inventory_disposals.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("inventory_items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("item_condition_at_disposal", sa.String(20)),
        sa.Column("item_notes_at_disposal", sa.Text),
    )

    # ── Alert routes ──────────────────────────────────────────────────────
    op.create_table(
        "inventory_alert_routes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("alert_type", sa.String(50), nullable=False, unique=True),
        # JSONB array of email strings — easier UI handoff than text[]
        sa.Column("recipients", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("cc_actor", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Permissions ───────────────────────────────────────────────────────
    perms = [
        ("inventory.view", "View inventory items"),
        ("inventory.edit", "Create, update, transfer, adjust inventory items"),
        ("inventory.audit.run", "Run cycle-count audits"),
        ("inventory.checkout.manage", "Manage checkout / loan ledger"),
        ("inventory.categories.manage", "Manage inventory categories"),
        ("inventory.enrich", "Trigger enrichment lookups (rate-limit-sensitive)"),
        ("inventory.orders.view", "View pending orders"),
        ("inventory.orders.receive", "Receive against pending orders"),
        ("inventory.orders.manage", "Cancel, reparse, manually edit pending orders"),
        ("inventory.treasurer.forward", "Trigger or re-trigger treasurer forwards"),
        ("inventory.dispose", "Create disposal records"),
        ("inventory.disposal.verify", "Second-user wipe verification (must differ from disposer)"),
        ("inventory.disposal.reverse", "Un-dispose (high-visibility action)"),
        ("inventory.labels.print", "Generate Avery label PDFs"),
    ]
    for action, desc in perms:
        op.execute(
            f"""INSERT INTO permissions (action, description, is_student_sensitive)
                VALUES ('{action}', '{desc.replace("'", "''")}', false)
                ON CONFLICT (action) DO NOTHING"""
        )
        op.execute(
            f"""INSERT INTO role_permissions (role_id, permission_id)
                SELECT r.id, p.id FROM roles r, permissions p
                WHERE r.name = 'admin' AND p.action = '{action}'
                ON CONFLICT DO NOTHING"""
        )

    # ── Seed default alert routes (empty recipient list, disabled) ────────
    for alert_type in (
        "reorder",
        "overdue_loan",
        "order_received",
        "disposal_pending_verification",
        "treasurer_forward_failed",
    ):
        op.execute(
            f"""INSERT INTO inventory_alert_routes (alert_type, recipients, cc_actor, enabled)
                VALUES ('{alert_type}', '[]'::jsonb, false, false)
                ON CONFLICT (alert_type) DO NOTHING"""
        )

    # ── Seed a starter category set ───────────────────────────────────────
    starter_categories = [
        ("Switch", "network", False, True),
        ("Access Point", "wifi", False, True),
        ("Server", "server", False, True),
        ("Workstation", "monitor", False, True),
        ("Laptop", "laptop", True, True),
        ("Monitor", "monitor", False, False),
        ("Projector", "projector", False, True),
        ("UPS", "battery", False, False),
        ("Printer", "printer", False, True),
        ("Phone", "phone", False, False),
        ("Cable", "cable", False, False),
        ("Adapter", "plug", False, False),
        ("Spare Part", "tool", False, False),
        ("Consumable", "package", False, False),
        ("Tool", "tool", True, False),
    ]
    for i, (name, icon, loanable, capitalized) in enumerate(starter_categories):
        op.execute(
            f"""INSERT INTO inventory_categories
                (name, icon, default_is_loanable, default_is_capitalized, sort_order, archived)
                VALUES ('{name}', '{icon}', {loanable}, {capitalized}, {(i + 1) * 10}, false)
                ON CONFLICT (name) DO NOTHING"""
        )


def downgrade() -> None:
    op.drop_table("inventory_alert_routes")
    op.drop_table("inventory_disposal_items")
    op.drop_table("inventory_disposals")
    op.drop_table("inventory_shipping_docs")
    op.drop_table("inventory_enrichment_cache")
    op.drop_table("inventory_audit_scans")
    op.drop_table("inventory_audit_sessions")
    op.drop_table("inventory_checkouts")
    op.drop_table("inventory_location_history")
    op.drop_table("inventory_items")
    op.drop_table("inventory_pending_order_lines")
    op.drop_table("inventory_pending_orders")
    op.drop_table("inventory_categories")
    for action in (
        "inventory.view", "inventory.edit", "inventory.audit.run",
        "inventory.checkout.manage", "inventory.categories.manage",
        "inventory.enrich", "inventory.orders.view", "inventory.orders.receive",
        "inventory.orders.manage", "inventory.treasurer.forward",
        "inventory.dispose", "inventory.disposal.verify",
        "inventory.disposal.reverse", "inventory.labels.print",
    ):
        op.execute(f"DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE action = '{action}')")
        op.execute(f"DELETE FROM permissions WHERE action = '{action}'")
