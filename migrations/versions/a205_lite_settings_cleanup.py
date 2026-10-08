"""Clean up integration_configs rows for stripped integrations.

The lite fork keeps staff / roster / alerts and strips every other
module. Historical migrations seeded a bunch of integration_configs
rows for modules that no longer exist (paxton, halo, wave, chromebook,
etc.). The Settings UI filters its display list via LITE_KEPT_KEYS
in app/modules/settings/router.py — this migration drops the matching
DB rows so they don't linger as orphans operators might stumble on
via psql / backups.

Idempotent — if the rows aren't there, the DELETEs no-op.

Revision ID: a205_lite_settings_cleanup
Revises: a204_staff_photos
Create Date: 2026-10-08
"""
from typing import Sequence, Union
from alembic import op

revision: str = "a205_lite_settings_cleanup"
down_revision: Union[str, None] = "a204_staff_photos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirror of what's NOT in LITE_KEPT_KEYS. Keep in sync if the kept
# list changes.
STRIPPED_INTEGRATIONS = [
    "ad", "google_maps", "search", "halo", "paxton",
    "wave_server_1", "wave_server_2",
    "weather", "openweather",
    "camera_hikvision", "camera_hanwha", "rei",
    "nutrikids", "student_scan",
    "librenms", "network", "switch_ssh", "switch_telnet",
    "aruba_instant", "epson_projectors", "spectrum_outages",
    "google_chrome_printers", "canon_printers", "printers", "acus",
    "tickets", "chromebook", "chromebook_repair", "chromebook_parts",
    "grandstream", "voice", "transfinder",
    "drive", "inventory", "order_email", "treasurer_forward",
    "dhcp_ingest", "gemini",
]


def upgrade() -> None:
    for integ in STRIPPED_INTEGRATIONS:
        op.execute(
            "DELETE FROM integration_configs WHERE integration = '{}'".format(integ)
        )


def downgrade() -> None:
    # No restore — the dropped rows had no meaningful content in lite.
    pass
