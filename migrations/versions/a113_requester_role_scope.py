"""Tighten the `requester` role to ticket-only access.

The role originally got `dashboard.view` so requesters had a landing
page. With the new FMX-adjacent `/tickets/submit` dashboard, that's no
longer needed — and `dashboard.view` was pulling them into the main
Nexus dashboard, exposing modules they shouldn't see. Strip
`dashboard.view` so the post-login resolver routes them straight to
`/tickets/submit`.

Revision ID: a113_requester_role_scope
Revises: a112_chat_db_role
"""
from __future__ import annotations

from alembic import op


revision = "a113_requester_role_scope"
down_revision = "a112_chat_db_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions
        WHERE role_id = (SELECT id FROM roles WHERE name = 'requester')
          AND permission_id = (SELECT id FROM permissions WHERE action = 'dashboard.view')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT (SELECT id FROM roles WHERE name = 'requester'),
               (SELECT id FROM permissions WHERE action = 'dashboard.view')
        WHERE NOT EXISTS (
            SELECT 1 FROM role_permissions
            WHERE role_id = (SELECT id FROM roles WHERE name = 'requester')
              AND permission_id = (SELECT id FROM permissions WHERE action = 'dashboard.view')
        )
        """
    )
