"""Custom device-kind registry for the switch profile page.

Allows an operator to add new port-device categories without a
code change. The classifier merges these with the hardcoded builtin
list; the client fetches the full merged set at page load and uses
it for icon + label + filter chip rendering.

Table shape:
  kind          — short key (e.g. 'streaming', 'thermostat', 'iot')
  label         — human-readable name shown in UI
  icon_svg      — full <svg viewBox="..." ...>...</svg> markup
  oui_prefixes  — array of uppercase 6-char OUI prefixes ('B0A737' etc.)
  mfr_patterns  — array of lowercase manufacturer-name substrings
                   for fallback classification when OUI isn't in map
  created_by / created_at / updated_at

Revision ID: a198_device_kinds
Revises: a197_custom_sections_perm
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "a198_device_kinds"
down_revision = "a197_custom_sections_perm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_kind_definitions",
        sa.Column("kind", sa.String(30), primary_key=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("icon_svg", sa.Text, nullable=False),
        sa.Column("oui_prefixes", ARRAY(sa.String(6)), nullable=False,
                  server_default="{}"),
        sa.Column("mfr_patterns", ARRAY(sa.String(80)), nullable=False,
                  server_default="{}"),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.execute("""
        INSERT INTO permissions (action, description)
        VALUES ('network.device_kinds.manage',
                'Add / edit / remove custom device categories on the switch profile page')
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT 1, id FROM permissions WHERE action = 'network.device_kinds.manage'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM permissions WHERE action = 'network.device_kinds.manage'")
    op.drop_table("device_kind_definitions")
