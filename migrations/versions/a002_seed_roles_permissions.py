"""Seed roles and permissions.

Revision ID: a002_seed
Revises: a001_initial
Create Date: 2026-03-27
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a002_seed"
down_revision: Union[str, None] = "a001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Permission definitions: (action, description, is_student_sensitive)
PERMISSIONS = [
    ("dashboard.view", "View dashboard", False),
    ("staff.view", "View staff directory", False),
    ("staff.request.submit", "Submit onboard/offboard requests", False),
    ("staff.provision.execute", "Execute provisioning workflows", False),
    ("staff.settings.manage", "Manage staff settings", False),
    ("access.view", "View door events and history", False),
    ("access.door.open", "Open/close/hold doors", False),
    ("network.view", "View network devices and topology", False),
    ("network.switch.rename", "Rename switch ports", False),
    ("network.switch.vlan", "Apply VLAN templates", False),
    ("network.switch.poe_cycle", "Cycle PoE on ports", False),
    ("network.switch.locator", "Toggle locator LED", False),
    ("roster.view", "View roster change log", True),
    ("roster.import.run", "Trigger roster import", True),
    ("roster.students.view", "View student directory and profiles", True),
    ("roster.students.export", "Export student data CSV", True),
    ("roster.classlist.view", "View class lists", True),
    ("roster.guidance.view", "View guidance queue", True),
    ("roster.guidance.manage", "Manage guidance queue", True),
    ("roster.accounts.provision", "Provision/confirm student accounts", True),
    ("settings.manage", "Manage system settings", False),
    ("audit.view", "View audit log", False),
    ("chromebook.view", "View Chromebook inventory", False),
    ("chromebook.manage", "Manage Chromebook assignments and actions", False),
    ("docs.view", "View documentation and operations", False),
    ("docs.manage", "Manage docs, vendors, and contacts", False),
]

# Role definitions: (name, description, [permission_actions])
ROLES = [
    ("admin", "District IT administrator — full access", [p[0] for p in PERMISSIONS]),
    ("staff_viewer", "Staff directory read access", [
        "dashboard.view", "staff.view", "audit.view",
    ]),
    ("hr", "HR intake — submit requests only", [
        "staff.request.submit",
    ]),
    ("network_viewer", "Network monitoring read access", [
        "dashboard.view", "network.view", "audit.view",
    ]),
    ("network_admin", "Network monitoring + control actions", [
        "dashboard.view", "network.view", "audit.view",
        "network.switch.rename", "network.switch.vlan",
        "network.switch.poe_cycle", "network.switch.locator",
    ]),
    ("access_viewer", "Door access read access", [
        "dashboard.view", "access.view", "audit.view",
    ]),
    ("access_admin", "Door access + control", [
        "dashboard.view", "access.view", "access.door.open", "audit.view",
    ]),
    ("roster_viewer", "Roster read access (student data)", [
        "dashboard.view", "roster.view", "roster.students.view",
        "roster.guidance.view", "audit.view",
    ]),
    ("principal", "Building principal — student data for own building", [
        "dashboard.view", "roster.view", "roster.students.view",
        "roster.classlist.view", "roster.guidance.view", "audit.view",
    ]),
    ("guidance_counselor", "Guidance counselor — own building queue + class lists", [
        "roster.guidance.view", "roster.guidance.manage",
        "roster.students.view", "roster.classlist.view",
    ]),
    ("chromebook_admin", "Chromebook management — building-scoped device maintenance", [
        "dashboard.view", "chromebook.view", "chromebook.manage", "audit.view",
    ]),
]


def upgrade() -> None:
    conn = op.get_bind()

    # Insert permissions
    perm_table = sa.table(
        "permissions",
        sa.column("action", sa.String),
        sa.column("description", sa.Text),
        sa.column("is_student_sensitive", sa.Boolean),
    )
    for action, description, is_sensitive in PERMISSIONS:
        conn.execute(perm_table.insert().values(
            action=action, description=description, is_student_sensitive=is_sensitive,
        ))

    # Load permission IDs
    perm_ids = {}
    for row in conn.execute(sa.text("SELECT id, action FROM permissions")):
        perm_ids[row[1]] = row[0]

    # Insert roles
    role_table = sa.table(
        "roles",
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
    )
    for role_name, description, _ in ROLES:
        conn.execute(role_table.insert().values(name=role_name, description=description))

    # Load role IDs
    role_ids = {}
    for row in conn.execute(sa.text("SELECT id, name FROM roles")):
        role_ids[row[1]] = row[0]

    # Insert role → permission mappings
    rp_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission_id", sa.Integer),
    )
    for role_name, _, perm_actions in ROLES:
        rid = role_ids[role_name]
        for action in perm_actions:
            pid = perm_ids.get(action)
            if pid:
                conn.execute(rp_table.insert().values(role_id=rid, permission_id=pid))


def downgrade() -> None:
    """
    Scoped downgrade — only removes rows introduced by this migration.
    Operator-created roles and permissions added after deployment survive.
    """
    import sqlalchemy as sa

    permission_actions = [p[0] for p in PERMISSIONS]
    role_names = [r[0] for r in ROLES]

    # Remove role→permission links for our roles only
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE role_id IN "
            "(SELECT id FROM roles WHERE name = ANY(:names))"
        ).bindparams(names=role_names)
    )

    # Remove our roles only
    op.execute(
        sa.text("DELETE FROM roles WHERE name = ANY(:names)")
        .bindparams(names=role_names)
    )

    # Remove our permissions only
    op.execute(
        sa.text("DELETE FROM permissions WHERE action = ANY(:actions)")
        .bindparams(actions=permission_actions)
    )
