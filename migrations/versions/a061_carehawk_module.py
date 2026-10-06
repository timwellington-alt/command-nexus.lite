"""Carehawk (bell schedule + PA) module — permissions + building registry.

Adds `carehawk.view` / `carehawk.manage` permissions, grants them to
admin, and seeds the `carehawk.buildings` integration_config JSON with
the three confirmed CH1000 units (example buildings). Settings-
write is deliberately NOT part of this module's protocol — use the
vendor Windows tool for port/zone config changes.

Revision ID: a061_carehawk_module
Revises: a060_pending_loc_hints
Create Date: 2026-04-24
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a061_carehawk_module"
down_revision: Union[str, None] = "a060_pending_loc_hints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BUILDINGS_JSON = json.dumps([
    {"id": 1, "name": "PES", "aliases": [], "ip": "192.0.2.1", "default_tone_index": 38},
    {"id": 2, "name": "PHS", "aliases": [], "ip": "192.0.2.1", "default_tone_index": 25},
    {"id": 3, "name": "EPE", "aliases": [], "ip": "192.0.2.1", "default_tone_index": 37},
], separators=(",", ":"))


def upgrade() -> None:
    # ── Permissions ────────────────────────────────────────────────
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES
            ('carehawk.view',   'View bell schedules and tones',        false),
            ('carehawk.manage', 'Edit and save bell schedules to units', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name = 'admin'
          AND p.action IN ('carehawk.view', 'carehawk.manage')
        ON CONFLICT DO NOTHING
    """)

    # ── Building registry (JSON in integration_configs) ────────────
    # Use bindparam so the colons in the JSON aren't treated as
    # SQLAlchemy named-parameter markers.
    op.execute(
        sa.text("""
            INSERT INTO integration_configs (integration, key, value, is_secret_ref, updated_by)
            VALUES ('carehawk', 'buildings', :json, FALSE, 'migration:a061')
            ON CONFLICT (integration, key) DO NOTHING
        """).bindparams(json=_BUILDINGS_JSON)
    )


def downgrade() -> None:
    op.execute("""
        DELETE FROM integration_configs
        WHERE integration = 'carehawk' AND key = 'buildings'
    """)
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions WHERE action IN ('carehawk.view','carehawk.manage')
        )
    """)
    op.execute("""
        DELETE FROM permissions WHERE action IN ('carehawk.view','carehawk.manage')
    """)
