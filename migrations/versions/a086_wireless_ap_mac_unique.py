"""Stable IDs for wireless_ap_cache via UNIQUE(mac).

The refresh_wireless_clients worker used to DELETE+INSERT the entire
table every 2 minutes, churning the auto-incrementing `id` and
orphaning any FloorPlanPin rows that referenced it (APs would
disappear from the floor plan after the next sync). MAC is the natural
stable key — adding UNIQUE on it lets the worker switch to ON CONFLICT
DO UPDATE so existing rows keep their `id` across polls.

Revision ID: a086_wireless_ap_mac_unique
Revises: a085_camera_fov_range
Create Date: 2026-05-04
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a086_wireless_ap_mac_unique"
down_revision: Union[str, None] = "a085_camera_fov_range"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pre-clean: drop any rows with NULL or duplicate MAC so the unique
    # constraint can be added. Keep newest row per MAC.
    op.execute("DELETE FROM wireless_ap_cache WHERE mac IS NULL OR mac = ''")
    op.execute("""
        DELETE FROM wireless_ap_cache a USING wireless_ap_cache b
        WHERE a.id < b.id AND a.mac = b.mac
    """)
    op.create_unique_constraint(
        "uq_wireless_ap_cache_mac", "wireless_ap_cache", ["mac"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_wireless_ap_cache_mac", "wireless_ap_cache", type_="unique")
