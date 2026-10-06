"""Damage reports — incidents + confirmed items + item photos.

Data model for /operations/damage-reports:

- ``damage_incidents`` — one per event (Aug 16 storm, vandalism at PES,
  water damage in the auditorium, etc.). Type is free-text so we can
  cover storms, vandalism, floods, theft, wear-out, whatever.
- ``damage_items`` — one confirmed damaged thing per row. Techs enter
  these from the field as they verify damage. May reference a Nexus
  network device (device_id) or be totally free-form (physical door
  hardware, cable-plant damage, etc.). ``status`` tracks the workflow:
  ``pending`` (auto-scan candidate, not yet field-verified),
  ``confirmed`` (physically damaged, needs replacement),
  ``dismissed`` (looked at it, actually fine).
- ``damage_item_photos`` — optional attachments per item. Files live on
  disk at ``/app/data/damage_photos/{incident_id}/{item_id}/{filename}``.

Revision ID: a167_damage_reports
Revises: a166_door_action_timing
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a167_damage_reports"
down_revision = "a166_door_action_timing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "damage_incidents",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("incident_type", sa.String(60), nullable=False,
                  server_default="other"),
        sa.Column("incident_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="open"),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text),
    )
    op.create_index("ix_damage_incidents_status", "damage_incidents", ["status"])
    op.create_index("ix_damage_incidents_incident_date",
                    "damage_incidents", ["incident_date"])
    op.create_check_constraint(
        "ck_damage_incidents_status",
        "damage_incidents",
        "status IN ('open', 'closed', 'archived')",
    )

    op.create_table(
        "damage_items",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.Integer,
                  sa.ForeignKey("damage_incidents.id", ondelete="CASCADE"),
                  nullable=False),
        # What is it?
        sa.Column("item_category", sa.String(60), nullable=False),
        sa.Column("model", sa.String(200)),
        sa.Column("serial", sa.String(120)),
        # Where is it?
        sa.Column("location", sa.String(200)),
        sa.Column("building", sa.String(60)),
        # Optional link back to a Nexus device (LibreNMS device_id or
        # switch port token) — makes the "which item?" question trivial
        # for network-tracked gear. Free-text items just leave this null.
        sa.Column("device_ref", sa.String(200)),
        # Description of the damage itself
        sa.Column("damage_description", sa.Text),
        # Workflow: pending (auto-scan candidate) → confirmed | dismissed
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("severity", sa.String(20)),  # minor|moderate|total-loss|nil
        # Financial info (only meaningful for confirmed items)
        sa.Column("replacement_cost_cents", sa.BigInteger),
        sa.Column("labor_cost_cents", sa.BigInteger),
        # Provenance
        sa.Column("source", sa.String(30), nullable=False,
                  server_default="manual"),  # 'manual' | 'auto_scan'
        sa.Column("scan_evidence", postgresql.JSONB),  # what the scan saw
        sa.Column("notes", sa.Text),
        # Audit
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_by", sa.String(255)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_damage_items_incident", "damage_items", ["incident_id"])
    op.create_index("ix_damage_items_status", "damage_items", ["status"])
    op.create_check_constraint(
        "ck_damage_items_status",
        "damage_items",
        "status IN ('pending', 'confirmed', 'dismissed')",
    )
    op.create_check_constraint(
        "ck_damage_items_source",
        "damage_items",
        "source IN ('manual', 'auto_scan')",
    )

    op.create_table(
        "damage_item_photos",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer,
                  sa.ForeignKey("damage_items.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(80)),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("caption", sa.Text),
        sa.Column("uploaded_by", sa.String(255), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_damage_item_photos_item", "damage_item_photos", ["item_id"])

    # ── Permissions ────────────────────────────────────────────────
    for action, desc in [
        ("operations.damage_reports.view",
         "View damage incidents and confirmed damage reports"),
        ("operations.damage_reports.manage",
         "Create / edit damage incidents and items, run auto-scan, upload photos"),
    ]:
        op.execute(f"""
            INSERT INTO permissions (action, description, is_student_sensitive)
            VALUES ('{action}', '{desc}', false)
            ON CONFLICT (action) DO NOTHING
        """)
        op.execute(f"""
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id FROM roles r, permissions p
            WHERE r.name = 'admin' AND p.action = '{action}'
            ON CONFLICT DO NOTHING
        """)


def downgrade() -> None:
    for action in (
        "operations.damage_reports.view",
        "operations.damage_reports.manage",
    ):
        op.execute(f"""
            DELETE FROM role_permissions WHERE permission_id IN
                (SELECT id FROM permissions WHERE action = '{action}')
        """)
        op.execute(f"DELETE FROM permissions WHERE action = '{action}'")
    op.drop_table("damage_item_photos")
    op.drop_table("damage_items")
    op.drop_table("damage_incidents")
