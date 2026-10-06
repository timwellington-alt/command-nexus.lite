"""Seed the chat.memory.team_write permission for shared team-memory writes.

scope='team' on save_memory writes a file readable by every chat.admin user
(and by Tim's terminal Claude Code). That's a stored prompt-injection
primitive across tenants — any chat.admin could otherwise plant content
that becomes part of everyone else's system prompt. Gating it behind a
distinct permission lets the admin role still drive personal saves
freely while reserving cross-tenant writes for trusted users.

Granted by default to `admin` only. Add `network_admin` etc. on a
per-user basis as needed.

Revision ID: a137_chat_team_write_perm
Revises: a136_printer_smtp_step
"""
from __future__ import annotations

from alembic import op


revision = "a137_chat_team_write_perm"
down_revision = "a136_printer_smtp_step"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES ('chat.memory.team_write', 'Write to shared team chat memory (visible to all admins)', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r, permissions p
        WHERE r.name = 'admin' AND p.action = 'chat.memory.team_write'
        ON CONFLICT ON CONSTRAINT role_permissions_role_id_permission_id_key DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id = (SELECT id FROM permissions WHERE action = 'chat.memory.team_write')
    """)
    op.execute("DELETE FROM permissions WHERE action = 'chat.memory.team_write'")
