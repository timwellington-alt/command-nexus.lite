"""Extend oui_registry to support MA-M (28-bit) and MA-S (36-bit) prefixes.

IEEE's free CSV endpoint started returning 418 in mid-2026 so the
monthly refresh stopped working. Replacement source is Wireshark's
manuf file which bundles MA-L (/24), MA-M (/28), MA-S (/36), and IAB
entries plus a hand-curated short-name column.

Adds `prefix_bits` (24/28/36) so the lookup can prefer the longest
matching prefix — a MA-S row beats a MA-L row whose prefix is just the
"IEEE Registration Authority" parent block.

Revision ID: a126_oui_registry_bits
Revises: a125_manual_network_devices
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a126_oui_registry_bits"
down_revision = "a125_manual_network_devices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oui_registry",
        sa.Column("prefix_bits", sa.SmallInteger, nullable=False, server_default="24"),
    )
    op.add_column(
        "oui_registry",
        sa.Column("short_name", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_oui_registry_prefix_bits", "oui_registry", ["prefix_bits"],
    )


def downgrade() -> None:
    op.drop_index("ix_oui_registry_prefix_bits", table_name="oui_registry")
    op.drop_column("oui_registry", "short_name")
    op.drop_column("oui_registry", "prefix_bits")
