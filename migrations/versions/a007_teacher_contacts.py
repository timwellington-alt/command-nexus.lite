"""Add roster.students.contacts.view permission and teacher role.

Teachers see T4 contact fields (parent/guardian, phone, parent email)
scoped to their assigned sections via student_teachers.teacher_email.

Revision ID: a007_teacher_contacts
Revises: a006_roster_module
Create Date: 2026-03-28
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a007_teacher_contacts"
down_revision: Union[str, None] = "a006_roster"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_PERMISSIONS = [
    ("roster.students.contacts.view",
     "View student T4 contact fields (parent/guardian, phone, parent email)",
     True),
]

# Roles that get the new contacts permission and their scope context
# admin already gets all permissions via seeds — only add to principal + guidance here
CONTACT_ROLES = ["admin", "principal", "guidance_counselor"]

NEW_ROLES = [
    ("teacher", "Teacher — own sections class list + section-scoped contact info"),
]

TEACHER_PERMISSIONS = ["roster.classlist.view", "roster.students.contacts.view"]


def upgrade() -> None:
    conn = op.get_bind()

    # Insert new permission
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

    # Load permission IDs
    rows = conn.execute(sa.text("SELECT id, action FROM permissions"))
    perm_ids = {row[1]: row[0] for row in rows}

    # Grant contacts.view to existing authorized roles (admin, principal, guidance)
    rp_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission_id", sa.Integer),
    )
    contacts_perm_id = perm_ids.get("roster.students.contacts.view")
    if contacts_perm_id:
        role_rows = conn.execute(
            sa.text("SELECT id, name FROM roles WHERE name = ANY(:names)").bindparams(
                names=CONTACT_ROLES
            )
        )
        for role_id, role_name in role_rows:
            conn.execute(rp_table.insert().values(
                role_id=role_id, permission_id=contacts_perm_id
            ))

    # Insert teacher role
    role_table = sa.table(
        "roles",
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
    )
    for role_name, description in NEW_ROLES:
        conn.execute(role_table.insert().values(name=role_name, description=description))

    # Assign permissions to teacher role
    teacher_row = conn.execute(
        sa.text("SELECT id FROM roles WHERE name = 'teacher'")
    ).fetchone()
    if teacher_row:
        teacher_id = teacher_row[0]
        for action in TEACHER_PERMISSIONS:
            pid = perm_ids.get(action)
            if pid:
                conn.execute(rp_table.insert().values(
                    role_id=teacher_id, permission_id=pid
                ))


def downgrade() -> None:
    conn = op.get_bind()

    # Remove teacher role and its permissions
    conn.execute(sa.text(
        "DELETE FROM role_permissions WHERE role_id IN "
        "(SELECT id FROM roles WHERE name = 'teacher')"
    ))
    conn.execute(sa.text("DELETE FROM roles WHERE name = 'teacher'"))

    # Remove contacts.view from all roles
    conn.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE action = 'roster.students.contacts.view')"
    ))
    conn.execute(sa.text(
        "DELETE FROM permissions WHERE action = 'roster.students.contacts.view'"
    ))
