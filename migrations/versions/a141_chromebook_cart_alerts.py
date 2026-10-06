"""Add chromebook_cart_meta + chromebook_wander_state, seed cart-alert settings.

Phase 1 of the Cart Management extension:
    - chromebook_cart_meta: one row per Cart OU, tracks its home AP.
    - chromebook_wander_state: one row per wandered device, tracks the
      cursor the alert job needs (first_wander_at → escalated_at).
    - Seeds three JSON-blob settings under the existing `chromebook`
      integration group so per-building EOD times, escalation days,
      and building admin emails are editable in Settings without a
      schema change per building.

Revision ID: a141_chromebook_cart_alerts
Revises: a140_direct_print_printers
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "a141_chromebook_cart_alerts"
down_revision = "a140_direct_print_printers"
branch_labels = None
depends_on = None


SEED_SETTINGS = [
    (
        "wander_eod_times",
        json.dumps({"PES": "15:30", "EPE": "15:30", "PHS": "15:00"}),
        "Per-building EOD time (HH:MM local) when the cart-wander check fires — JSON keyed by building.",
    ),
    (
        "wander_escalation_days",
        "2",
        "Days a device must remain wandered before escalating to the building admin.",
    ),
    (
        "wander_admin_emails",
        json.dumps({
            "PES": "admin@yourdistrict.org",
            "EPE": "admin@yourdistrict.org",
            "PHS": "admin@yourdistrict.org",
        }),
        "Per-building admin email for wander-escalation notices — JSON keyed by building.",
    ),
]


def upgrade() -> None:
    # ── chromebook_cart_meta ───────────────────────────────────────────
    op.create_table(
        "chromebook_cart_meta",
        sa.Column("ou_path", sa.Text, primary_key=True),
        sa.Column("building", sa.String(50), nullable=False, index=True),
        sa.Column("room", sa.String(50), nullable=False),
        sa.Column("home_ap_name", sa.String(200), nullable=True),
        # 'name' = matched from AP naming convention, 'plurality' = derived
        # from where most devices sit during the school day, 'manual' =
        # operator-set from the drill-in page (never overwritten by the
        # derivation job).
        sa.Column("home_ap_source", sa.String(20), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(255), nullable=True),
    )

    # ── chromebook_wander_state ────────────────────────────────────────
    op.create_table(
        "chromebook_wander_state",
        sa.Column("device_id", sa.String(255), primary_key=True),
        sa.Column("ou_path", sa.Text, nullable=False, index=True),
        sa.Column("first_wander_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_wander_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_chromebook_wander_state_first_wander",
        "chromebook_wander_state",
        ["first_wander_at"],
    )

    # ── Seed settings (idempotent — skip if key already present) ───────
    # Insert one row per key under the existing `chromebook` integration
    # group so they show up in the same Settings panel as the other
    # chromebook keys (label config, room_pods, etc.).
    conn = op.get_bind()
    for key, value, _desc in SEED_SETTINGS:
        conn.execute(
            sa.text(
                """
                INSERT INTO integration_configs (integration, key, value, is_secret_ref, updated_by)
                VALUES ('chromebook', :key, :value, false, 'a141_migration')
                ON CONFLICT (integration, key) DO NOTHING
                """
            ),
            {"key": key, "value": value},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for key, _v, _d in SEED_SETTINGS:
        conn.execute(
            sa.text(
                "DELETE FROM integration_configs "
                "WHERE integration = 'chromebook' AND key = :key"
            ),
            {"key": key},
        )
    op.drop_index("ix_chromebook_wander_state_first_wander", "chromebook_wander_state")
    op.drop_table("chromebook_wander_state")
    op.drop_table("chromebook_cart_meta")
