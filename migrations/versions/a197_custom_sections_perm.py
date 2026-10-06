"""Add roster.custom_sections.manage permission.

Standalone perm gates the Settings surface + manual sync button for
the Clever custom-sections pipeline. Admin gets it by default via
the app's existing role_permissions grant path.

Revision ID: a197_custom_sections_perm
Revises: a196_att_report_recips
"""
from __future__ import annotations

from alembic import op

revision = "a197_custom_sections_perm"
down_revision = "a196_att_report_recips"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO permissions (action, description)
        VALUES ('roster.custom_sections.manage',
                'Manage Clever custom-sections pipeline (sheet-sourced sections + enrollments, SFTP push to Clever)')
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT 1, id FROM permissions WHERE action = 'roster.custom_sections.manage'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM permissions WHERE action = 'roster.custom_sections.manage'")
