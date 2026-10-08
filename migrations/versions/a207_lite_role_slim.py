"""Trim roles to the lite set: admin + viewer only.

Primary nexus ships ~15 roles across network / access / chromebook /
transportation / tickets / inventory. The lite build strips every
module except staff + roster + alerts + settings, so most of those
roles have no permissions to grant.

Lite ships two roles:
  admin  — full access to every seeded permission (same as primary)
  viewer — read-only on staff + roster + dashboard + audit

Receiving districts can add more granular roles later via SQL
(a UI for role CRUD is a future feature).

Idempotent — if roles were already pruned no-ops; if any
user_roles reference a dropped role, their assignment is removed
(FK CASCADE handles that automatically).

Revision ID: a207_lite_role_slim
Revises: a206_local_user_totp
Create Date: 2026-10-08
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a207_lite_role_slim"
down_revision: Union[str, None] = "a206_local_user_totp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Permissions granted to the lite 'viewer' role. Only actions that
# exist in a shipped module — any permission referenced here that
# hasn't been seeded is silently skipped by the INSERT ... SELECT
# join below.
VIEWER_PERMISSIONS = [
    "dashboard.view",
    "staff.view",
    "audit.view",
    "roster.view",
    "roster.students.view",
    "roster.classlist.view",
    "roster.guidance.view",
    "roster.students.contacts.view",
    "roster.analytics.view",
]

KEEP_ROLES = {"admin", "viewer"}


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Delete every role that isn't in the keep set. user_roles has
    #    ON DELETE CASCADE to roles.id (see a001 initial schema) so
    #    stale assignments go with it.
    conn.execute(sa.text("""
        DELETE FROM roles WHERE name NOT IN :keep
    """).bindparams(sa.bindparam("keep", KEEP_ROLES, expanding=True)))

    # 2. Make sure the two we keep exist (upsert).
    conn.execute(sa.text("""
        INSERT INTO roles (name, description)
        VALUES ('admin', 'District IT administrator — full access')
        ON CONFLICT (name) DO NOTHING
    """))
    conn.execute(sa.text("""
        INSERT INTO roles (name, description)
        VALUES ('viewer', 'Read-only access to staff + student directories')
        ON CONFLICT (name) DO NOTHING
    """))

    # 3. Reset role_permissions for both. For admin: grant everything.
    #    For viewer: grant the lite-scoped read actions.
    conn.execute(sa.text("""
        DELETE FROM role_permissions WHERE role_id IN
            (SELECT id FROM roles WHERE name IN ('admin', 'viewer'))
    """))

    # admin → every seeded permission
    conn.execute(sa.text("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
        WHERE r.name = 'admin'
    """))

    # viewer → the subset above, only for permissions that exist
    conn.execute(sa.text("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r JOIN permissions p ON p.action = ANY(:actions)
        WHERE r.name = 'viewer'
    """).bindparams(actions=VIEWER_PERMISSIONS))


def downgrade() -> None:
    # No restore — the dropped roles had no shipped-module permissions
    # to grant anyway. Re-run a002 to resurrect the full seed.
    pass
