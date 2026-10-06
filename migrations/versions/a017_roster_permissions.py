"""Add new roster permissions for changes, compliance, and Google management.

Revision ID: a017_roster_permissions
Revises: a016_roster_extras
Create Date: 2026-03-29
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a017_roster_permissions"
down_revision: Union[str, None] = "a016_roster_extras"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [
    ("roster.changes.view", "View roster change log", True),
    ("roster.compliance.view", "View email compliance issues", True),
    ("roster.compliance.export", "Export compliance violations CSV", True),
    ("roster.compliance.manage", "Manage email ignore list", True),
    ("roster.google.view", "View Google account changelog", True),
    ("roster.google.manage", "Revert Google changes, resolve duplicates", True),
    ("roster.nutrikids.export", "Export NutriKids format data", True),
]


def upgrade() -> None:
    conn = op.get_bind()

    perm_table = sa.table(
        "permissions",
        sa.column("action", sa.String),
        sa.column("description", sa.Text),
        sa.column("is_student_sensitive", sa.Boolean),
    )
    for action, description, is_sensitive in NEW_PERMISSIONS:
        conn.execute(perm_table.insert().values(
            action=action, description=description,
            is_student_sensitive=is_sensitive,
        ))

    # Grant all new permissions to admin role
    rows = conn.execute(sa.text("SELECT id, action FROM permissions"))
    perm_ids = {row[1]: row[0] for row in rows}

    admin_row = conn.execute(
        sa.text("SELECT id FROM roles WHERE name = 'admin'")
    ).fetchone()

    if admin_row:
        rp_table = sa.table(
            "role_permissions",
            sa.column("role_id", sa.Integer),
            sa.column("permission_id", sa.Integer),
        )
        for action, _, _ in NEW_PERMISSIONS:
            pid = perm_ids.get(action)
            if pid:
                conn.execute(rp_table.insert().values(
                    role_id=admin_row[0], permission_id=pid
                ))


def downgrade() -> None:
    actions = [p[0] for p in NEW_PERMISSIONS]
    op.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE action = ANY(:actions))"
    ).bindparams(actions=actions))
    op.execute(sa.text(
        "DELETE FROM permissions WHERE action = ANY(:actions)"
    ).bindparams(actions=actions))
