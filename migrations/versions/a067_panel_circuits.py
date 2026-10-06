"""Panel circuits + upstream-feed relationship.

`inventory_items.fed_from_item_id` lets a downstream item (sub-panel,
boiler, AHU) point at its upstream feeder. Self-FK with ON DELETE SET
NULL — deleting a feeder doesn't cascade through the building.

`inventory_panel_circuits` is the per-circuit manifest of an electrical
panel. One row per circuit number; multi-pole breakers are represented
by a single row whose `poles` field is 2 or 3 (you'd still create one
row per visible circuit number for clarity, with poles set on each).
Spare-on-hand and supplier links live in `inventory_replacement_parts`
to avoid duplicating supplier URLs across N circuits using the same
breaker model.

Revision ID: a067_panel_circuits
Revises: a066_inventory_pending_intent
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a067_panel_circuits"
down_revision: Union[str, None] = "a066_inventory_pending_intent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_items",
        sa.Column(
            "fed_from_item_id",
            sa.Integer(),
            sa.ForeignKey("inventory_items.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "ix_inventory_items_fed_from", "inventory_items", ["fed_from_item_id"],
        postgresql_where=sa.text("fed_from_item_id IS NOT NULL"),
    )

    op.create_table(
        "inventory_panel_circuits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(),
                  sa.ForeignKey("inventory_items.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("circuit_number", sa.Integer(), nullable=False),
        sa.Column("area_served", sa.Text()),
        sa.Column("leg", sa.String(20)),
        sa.Column("voltage", sa.SmallInteger()),
        sa.Column("amperage", sa.SmallInteger()),
        sa.Column("poles", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("breaker_manufacturer", sa.String(100)),
        sa.Column("breaker_model", sa.String(100)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("item_id", "circuit_number", name="uq_panel_circuit_per_item"),
        sa.CheckConstraint("circuit_number >= 1 AND circuit_number <= 200",
                           name="ck_panel_circuit_number_range"),
        sa.CheckConstraint("poles BETWEEN 1 AND 3", name="ck_panel_circuit_poles"),
    )


def downgrade() -> None:
    op.drop_table("inventory_panel_circuits")
    op.drop_index("ix_inventory_items_fed_from", table_name="inventory_items")
    op.drop_column("inventory_items", "fed_from_item_id")
