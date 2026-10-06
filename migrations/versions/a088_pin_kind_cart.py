"""Allow 'cart' in floor_plan_pins.target_kind.

Adds chromebook carts to the pin-kind allowlist alongside ap/door/phone.
The Python-side ALLOWED_PIN_KINDS already includes 'cart'; this brings
the DB CHECK constraint in sync.

Revision ID: a088_pin_kind_cart
Revises: a087_wireless_ap_name_unique
Create Date: 2026-05-04
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a088_pin_kind_cart"
down_revision: Union[str, None] = "a087_wireless_ap_name_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE floor_plan_pins DROP CONSTRAINT ck_floor_plan_pins_kind")
    op.execute("""
        ALTER TABLE floor_plan_pins
        ADD CONSTRAINT ck_floor_plan_pins_kind
        CHECK (target_kind IN ('ap', 'door', 'phone', 'cart'))
    """)


def downgrade() -> None:
    op.execute("DELETE FROM floor_plan_pins WHERE target_kind = 'cart'")
    op.execute("ALTER TABLE floor_plan_pins DROP CONSTRAINT ck_floor_plan_pins_kind")
    op.execute("""
        ALTER TABLE floor_plan_pins
        ADD CONSTRAINT ck_floor_plan_pins_kind
        CHECK (target_kind IN ('ap', 'door', 'phone'))
    """)
