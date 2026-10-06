"""Security module — seed security.view permission.

Landing-page module for safety/security tools. No tables — just
the permission so the nav link and page are RBAC-gated.

Revision ID: a042_security_module
Revises: a041_voice_module
Create Date: 2026-04-13
"""

from typing import Sequence, Union
from alembic import op

revision: str = "a042_security_module"
down_revision: Union[str, None] = "a041_voice_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES ('security.view', 'View Security hub page', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name = 'admin'
          AND p.action = 'security.view'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions WHERE action = 'security.view'
        )
    """)
    op.execute("DELETE FROM permissions WHERE action = 'security.view'")
