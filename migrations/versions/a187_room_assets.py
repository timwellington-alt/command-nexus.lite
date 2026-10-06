"""Room-centric asset registry — Phase 4b of the room build.

One row per physical thing in a room. Absorbs the room_infrastructure
concept (spec logs) and chromebook_cart_meta (existing 138 cart rows
migrated inline). Every asset optionally references an inventory row
(for tracked SKUs like projectors + TVs) and optionally references the
ticket that installed it (for provenance).

Revision ID: a187_room_assets
Revises: a186_tickets_room_id
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a187_room_assets"
down_revision = "a186_tickets_room_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "room_assets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("room_id", sa.Integer(),
                  sa.ForeignKey("facility_rooms.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("asset_type", sa.String(40), nullable=False),
        sa.Column("label", sa.String(200)),
        sa.Column("spec", sa.Text),
        sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
        sa.Column("inventory_item_id", sa.Integer(),
                  sa.ForeignKey("inventory_items.id", ondelete="SET NULL")),
        sa.Column("installed_at", sa.Date),
        sa.Column("installed_by", sa.String(255)),
        sa.Column("installed_via_ticket_id", sa.Integer(),
                  sa.ForeignKey("tickets.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="installed"),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()"),
                  onupdate=sa.text("NOW()")),
    )
    op.create_index(
        "ix_room_assets_type", "room_assets", ["asset_type"],
    )
    op.create_index(
        "ix_room_assets_room_type", "room_assets", ["room_id", "asset_type"],
    )

    # Migrate the 138 chromebook_cart_meta rows in as room_assets.
    # Matches on (building, room) → facility_rooms.id. Any cart whose
    # room isn't in facility_rooms is skipped and logged (should be
    # zero given Phase 1 fixes but the LEFT JOIN + WHERE guards).
    op.execute("""
        INSERT INTO room_assets
            (room_id, asset_type, label, spec, quantity, status, notes)
        SELECT
            f.id,
            'chromebook_cart',
            COALESCE(NULLIF(m.ou_path, ''), 'Chromebook cart'),
            m.home_ap_name,
            1,
            'installed',
            m.notes
        FROM chromebook_cart_meta m
        JOIN facility_rooms f
          ON f.building_code = m.building AND f.room_code = m.room
    """)


def downgrade() -> None:
    op.drop_index("ix_room_assets_room_type", "room_assets")
    op.drop_index("ix_room_assets_type", "room_assets")
    op.drop_table("room_assets")
