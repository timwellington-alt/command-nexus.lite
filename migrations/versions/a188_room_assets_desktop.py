"""Room-asset ↔ chromebook link for teacher desktops.

Adds a nullable `chromebook_serial` column on `room_assets`, a partial
unique index so at most one installed `desktop` asset exists per serial,
and backfills existing ChromeFlex-OU teacher desktops into room_assets.

Prior state: teacher desktops were a derived query in the cart-detail
endpoint (chromebook_cache WHERE org_unit ILIKE '%chromeflex%' AND
annotated_asset_id/location matches BLD-ROOM). This migration promotes
them to first-class room_assets rows so they surface on the room detail
page like other assets and carry provenance + status.

Nightly reconcile keeps room_assets in sync with the live ChromeFlex OU
(see reconcile_teacher_desktops job).

Revision ID: a188_room_assets_desktop
Revises: a187_room_assets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a188_room_assets_desktop"
down_revision = "a187_room_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "room_assets",
        sa.Column("chromebook_serial", sa.String(50), nullable=True),
    )
    # Lookup index for the reconcile job (find asset by serial in O(log n)).
    op.create_index(
        "ix_room_assets_chromebook_serial",
        "room_assets",
        ["chromebook_serial"],
        postgresql_where=sa.text("chromebook_serial IS NOT NULL"),
    )
    # Enforce: at most ONE installed desktop asset per serial. Retired
    # rows accumulate for provenance; the constraint only bites active
    # rows. Same pattern is used elsewhere for soft-delete-friendly
    # uniqueness (see feedback_jsonb_null_vs_sql_null for the WHERE
    # clause discipline).
    op.execute("""
        CREATE UNIQUE INDEX ux_room_assets_installed_desktop_serial
        ON room_assets (chromebook_serial)
        WHERE chromebook_serial IS NOT NULL
          AND asset_type = 'desktop'
          AND status = 'installed'
    """)

    # Backfill: promote every ChromeFlex OU chromebook that matches a
    # facility_room via annotated_asset_id or annotated_location.
    #
    # Pattern reproduces the drill-in-detail heuristic: BLD-?ROOM followed
    # by a non-digit or end-of-string, so PES-241 doesn't accidentally
    # match a device tagged PES-2410. Building code and room code are
    # concatenated into the regex directly — they're alphanumeric only
    # in this district (PES, EPE, PHS, MAINT, ATH, and numeric/short
    # room codes like 241 / 2F / GYM), so no escaping is needed.
    #
    # DISTINCT ON collapses devices that match multiple rooms (rare —
    # would only happen if a device's annotated_location contains two
    # different building-room combos) to the alphabetically-first room.
    # Log any collapsed rows manually if we see the count drift.
    op.execute("""
        INSERT INTO room_assets
            (room_id, asset_type, label, spec, quantity,
             chromebook_serial, installed_at, installed_by,
             status, notes)
        SELECT DISTINCT ON (c.serial)
            f.id,
            'desktop',
            COALESCE(NULLIF(c.annotated_asset_id, ''), c.serial),
            c.model,
            1,
            c.serial,
            CURRENT_DATE,
            'system:desktop-backfill',
            'installed',
            'Auto-created 2026-09-01 from ChromeFlex OU (a188 backfill)'
        FROM chromebook_cache c
        JOIN facility_rooms f
          ON (
              c.annotated_asset_id ~
                ('\\y' || f.building_code || '-?' || f.room_code || '([^0-9]|$)')
              OR c.annotated_location ~
                ('\\y' || f.building_code || '-?' || f.room_code || '([^0-9]|$)')
          )
        WHERE (c.org_unit ILIKE '%chromeflex%' OR c.org_unit ILIKE '%chrome flex%')
          AND c.serial IS NOT NULL
          AND c.serial <> ''
        ORDER BY c.serial, f.building_code, f.room_code
    """)


def downgrade() -> None:
    # Drop only the backfilled rows so a re-upgrade re-creates the
    # same set without stacking duplicates on top of manual edits.
    op.execute("""
        DELETE FROM room_assets
         WHERE asset_type = 'desktop'
           AND installed_by = 'system:desktop-backfill'
    """)
    op.execute("DROP INDEX IF EXISTS ux_room_assets_installed_desktop_serial")
    op.drop_index(
        "ix_room_assets_chromebook_serial", table_name="room_assets"
    )
    op.drop_column("room_assets", "chromebook_serial")
