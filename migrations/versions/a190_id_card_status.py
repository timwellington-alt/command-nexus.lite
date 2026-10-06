"""ID card print queue status column.

Adds `id_card_status` on staff_queue so the provisioning path can queue
a real Paxton PVC ID card for print after temp-badge printing. States:
  - pending_photo  (queued, waiting for a passable photo)
  - ready          (photo processed OK — visible on /staff/id-cards)
  - printed        (operator confirmed the card came off the printer)
  - skipped        (operator dismissed — no card needed)

Nullable + no default: existing rows stay NULL and are ignored by the
new /staff/id-cards listing. Only rows explicitly opted-in by the
provisioning path get a value.

Revision ID: a190_id_card_status
Revises: a189_room_device_links
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a190_id_card_status"
down_revision = "a189_room_device_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "staff_queue",
        sa.Column("id_card_status", sa.String(30), nullable=True),
    )
    op.add_column(
        "staff_queue",
        sa.Column("id_card_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "staff_queue",
        sa.Column("id_card_printed_by", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_staff_queue_id_card_status",
        "staff_queue",
        ["id_card_status"],
        postgresql_where=sa.text("id_card_status IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_staff_queue_id_card_status", table_name="staff_queue")
    op.drop_column("staff_queue", "id_card_printed_by")
    op.drop_column("staff_queue", "id_card_updated_at")
    op.drop_column("staff_queue", "id_card_status")
