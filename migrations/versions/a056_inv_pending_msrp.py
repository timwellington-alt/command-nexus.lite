"""Inventory: photo-intake queue + MSRP column + LibreNMS device link.

Three additions:
  - inventory_items.msrp_cents — market reference value (Gemini ballpark);
    purchase_price_cents already covers actual paid cost.
  - inventory_items.libre_device_id — pointer back to LibreNMS device cache,
    unique nullable so the import is idempotent.
  - inventory_items.created_via — extend allowed values to include
    'email_intake' and 'libre_import'.
  - inventory_pending — pending-review queue for photos parsed by Gemini.
    Reviewer confirms → row is promoted into inventory_items.

Revision ID: a056_inv_pending_msrp
Revises: a055_recipient_channel_toggles
Create Date: 2026-04-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a056_inv_pending_msrp"
down_revision: Union[str, None] = "a055_recipient_channel_toggles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── inventory_items: msrp + libre pointer ────────────────────────────────
    op.add_column(
        "inventory_items",
        sa.Column("msrp_cents", sa.Integer(), nullable=True),
    )
    op.add_column(
        "inventory_items",
        sa.Column("libre_device_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_inventory_items_libre_device_id",
        "inventory_items",
        ["libre_device_id"],
        unique=True,
    )

    # Widen the created_via check to include the two new origins. SQLAlchemy
    # has no portable way to alter a check constraint, so drop+recreate.
    op.drop_constraint("ck_inventory_items_created_via", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_created_via",
        "inventory_items",
        "created_via IN ('manual', 'scan_commission', 'order_receipt', 'bulk_import', 'email_intake', 'libre_import')",
    )

    # ── inventory_pending ────────────────────────────────────────────────────
    op.create_table(
        "inventory_pending",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Source — stays NULL when a tech uploads a photo manually
        sa.Column("source_email_message_id", sa.String(120), nullable=True, unique=True),
        sa.Column("source_email_subject", sa.String(500), nullable=True),
        sa.Column("source_from_email", sa.String(255), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # The photo itself, stored inline. Quantities are small (a few/day).
        sa.Column("image_bytes", postgresql.BYTEA(), nullable=False),
        sa.Column("image_mime_type", sa.String(50), nullable=False, server_default="image/jpeg"),
        # Gemini output
        sa.Column("gemini_raw", postgresql.JSONB(), nullable=True),
        sa.Column("manufacturer", sa.String(100), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category_id", sa.Integer(),
                  sa.ForeignKey("inventory_categories.id", ondelete="SET NULL"), nullable=True),
        sa.Column("asset_tag", sa.String(50), nullable=True),
        # Multiple serials per photo are common (e.g. label batch shot)
        sa.Column("serials", postgresql.JSONB(), nullable=True),
        # Reviewer-only
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reviewer_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        # If confirmed, what item id(s) were created
        sa.Column("created_item_ids", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'confirmed', 'rejected')", name="ck_inventory_pending_status"),
    )
    op.create_index("ix_inventory_pending_status", "inventory_pending", ["status"])
    op.create_index("ix_inventory_pending_captured_at", "inventory_pending", ["captured_at"])


def downgrade() -> None:
    op.drop_index("ix_inventory_pending_captured_at", table_name="inventory_pending")
    op.drop_index("ix_inventory_pending_status", table_name="inventory_pending")
    op.drop_table("inventory_pending")

    op.drop_constraint("ck_inventory_items_created_via", "inventory_items", type_="check")
    op.create_check_constraint(
        "ck_inventory_items_created_via",
        "inventory_items",
        "created_via IN ('manual', 'scan_commission', 'order_receipt', 'bulk_import')",
    )

    op.drop_index("ix_inventory_items_libre_device_id", table_name="inventory_items")
    op.drop_column("inventory_items", "libre_device_id")
    op.drop_column("inventory_items", "msrp_cents")
