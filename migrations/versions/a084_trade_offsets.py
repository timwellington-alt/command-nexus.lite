"""Per (building, floor, trade) overlay alignment offset.

CAD-converter outputs (Acme + others) sometimes place trade-overlay
content at a slight translation offset from the architectural base SVG,
even when both share the same nominal viewbox. Rather than fix at
parse time, store a manual SVG-coord offset per trade and let the
viewer apply it via a wrapping group transform. Editable in the
viewer's edit mode.

Revision ID: a084_trade_offsets
Revises: a083_inv_extref_attrs
Create Date: 2026-05-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a084_trade_offsets"
down_revision: Union[str, None] = "a083_inv_extref_attrs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "facility_trade_offsets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("building_code", sa.String(20), nullable=False),
        sa.Column("floor", sa.String(20), nullable=False),
        sa.Column("trade_key", sa.String(40), nullable=False),
        sa.Column("dx", sa.Float(), nullable=False, server_default="0"),
        sa.Column("dy", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.UniqueConstraint("building_code", "floor", "trade_key",
                            name="uq_trade_offsets_bld_floor_trade"),
    )


def downgrade() -> None:
    op.drop_table("facility_trade_offsets")
