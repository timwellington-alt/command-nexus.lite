"""Switch wireless_ap_cache natural key from mac to name.

The Aruba Instant adapter doesn't expose MAC in `show ap` output (it
returns mac=None for every AP), so the MAC-keyed UPSERT introduced in
a086 silently skipped every row and the cache emptied. AP `name` is
populated by the adapter and stable across polls — use it as the
natural key for ID stability.

Revision ID: a087_wireless_ap_name_unique
Revises: a086_wireless_ap_mac_unique
Create Date: 2026-05-04
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a087_wireless_ap_name_unique"
down_revision: Union[str, None] = "a086_wireless_ap_mac_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The mac unique constraint never had a real chance to be useful
    # (mac was always NULL for Aruba APs). Drop it.
    op.drop_constraint("uq_wireless_ap_cache_mac", "wireless_ap_cache", type_="unique")

    # Pre-clean for the name unique add
    op.execute("DELETE FROM wireless_ap_cache WHERE name IS NULL OR name = ''")
    op.execute("""
        DELETE FROM wireless_ap_cache a USING wireless_ap_cache b
        WHERE a.id < b.id AND a.name = b.name
    """)
    op.create_unique_constraint(
        "uq_wireless_ap_cache_name", "wireless_ap_cache", ["name"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_wireless_ap_cache_name", "wireless_ap_cache", type_="unique")
    op.create_unique_constraint(
        "uq_wireless_ap_cache_mac", "wireless_ap_cache", ["mac"],
    )
