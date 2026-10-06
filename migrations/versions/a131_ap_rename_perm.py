"""Seed the network.ap.rename permission for the Aruba Instant rename action.

Grants the new permission to `admin` and `network_admin` roles. The action
pushes `ap-rename <old> <new>` to the Aruba Instant master over SSH from
the AP detail modal in the Network module.

Revision ID: a131_ap_rename_perm
Revises: a130_halo_alert_routing
"""
from __future__ import annotations

from alembic import op


revision = "a131_ap_rename_perm"
down_revision = "a130_halo_alert_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES ('network.ap.rename', 'Rename Aruba Instant APs', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r, permissions p
        WHERE r.name IN ('admin', 'network_admin')
          AND p.action = 'network.ap.rename'
        ON CONFLICT ON CONSTRAINT role_permissions_role_id_permission_id_key DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id = (SELECT id FROM permissions WHERE action = 'network.ap.rename')
    """)
    op.execute("DELETE FROM permissions WHERE action = 'network.ap.rename'")
