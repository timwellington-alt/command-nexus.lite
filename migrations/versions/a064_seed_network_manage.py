"""Seed `network.manage` permission.

The kernel has been referencing `network.manage` from six endpoints
(vlan-map reparse + override CRUD, vlan-template CRUD, manual switch
CRUD added in a063) but the permission row itself was never inserted —
so every protected action has been returning 403 to everyone.

This migration adds the permission and grants it to the admin role.
The corresponding role mapping for `network_admin` (if it exists) is
also granted so the feature works for that role.

Revision ID: a064_seed_network_manage
Revises: a063_network_manual_devices
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a064_seed_network_manage"
down_revision: Union[str, None] = "a063_network_manual_devices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES ('network.manage', 'Edit network configuration: VLAN map, manual switches, vlan templates', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name IN ('admin', 'network_admin')
          AND p.action = 'network.manage'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (SELECT id FROM permissions WHERE action = 'network.manage')
    """)
    op.execute("DELETE FROM permissions WHERE action = 'network.manage'")
