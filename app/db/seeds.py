"""
Seed data for roles and permissions — LOCAL DEV TOOLING ONLY.

Production uses Alembic migration a002_seed_roles_permissions.py.
This file is NOT called from app startup. Use only for local dev:
    python -m app.db.seeds
"""

import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Role, Permission, RolePermission

logger = logging.getLogger(__name__)

# Permission definitions: (action, description, is_student_sensitive)
PERMISSIONS = [
    # Dashboard
    ("dashboard.view", "View dashboard", False),

    # Staff
    ("staff.view", "View staff directory", False),
    ("staff.request.submit", "Submit onboard/offboard requests", False),
    ("staff.provision.execute", "Execute provisioning workflows", False),
    ("staff.settings.manage", "Manage staff settings", False),
    ("staff.queue.view", "View staff provisioning queue", False),
    ("staff.queue.edit", "Edit queue entries and mark ready", False),

    # Access Control
    ("access.view", "View door events and history", False),
    ("access.door.open", "Open/close/hold doors", False),

    # Network
    ("network.view", "View network devices and topology", False),
    ("network.switch.rename", "Rename switch ports", False),
    ("network.switch.vlan", "Apply VLAN templates", False),
    ("network.switch.poe_cycle", "Cycle PoE on ports", False),
    ("network.switch.locator", "Toggle locator LED", False),
    ("network.ap.rename", "Rename Aruba Instant APs", False),

    # Roster — general
    ("roster.view", "View roster change log", True),
    ("roster.import.run", "Trigger roster import", True),

    # Roster — student sensitive
    ("roster.students.view", "View student directory and profiles", True),
    ("roster.students.export", "Export student data CSV", True),
    ("roster.students.contacts.view", "View student T4 contact fields (parent, phone, email)", True),
    ("roster.classlist.view", "View class lists", True),
    ("roster.guidance.view", "View guidance queue", True),
    ("roster.guidance.manage", "Manage guidance queue (schedule/cancel)", True),
    ("roster.accounts.provision", "Provision/confirm student accounts", True),
    ("roster.changes.view", "View roster change log", True),
    ("roster.compliance.view", "View email compliance issues", True),
    ("roster.compliance.export", "Export compliance violations CSV", True),
    ("roster.compliance.manage", "Manage email ignore list", True),
    ("roster.google.view", "View Google account changelog", True),
    ("roster.google.manage", "Revert Google changes, resolve duplicates", True),
    ("roster.nutrikids.export", "Export NutriKids format data", True),

    # Settings
    ("settings.manage", "Manage system settings", False),

    # Audit
    ("audit.view", "View audit log", False),
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
        "network.ap.rename",
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
        "roster.students.contacts.view",
        "roster.classlist.view", "roster.guidance.view", "audit.view",
    ]),
    ("guidance_counselor", "Guidance counselor — own building queue + class lists", [
        "roster.guidance.view", "roster.guidance.manage",
        "roster.students.view", "roster.students.contacts.view",
        "roster.classlist.view",
    ]),
    ("teacher", "Teacher — own sections class list + section-scoped contact info", [
        "roster.classlist.view",
        "roster.students.contacts.view",
    ]),
    ("reception", "Building reception — queue editing and student directory", [
        "dashboard.view", "staff.view", "staff.queue.view", "staff.queue.edit",
        "roster.view", "roster.students.view",
    ]),
    # Transport staff don't operate Nexus — they use Fleet Watch (the
    # sibling dashcam app) which reads auth_groups from the Nexus
    # session. They need enough Nexus access to log in, see the
    # Transport nav link, and reach /transportation where the Fleet
    # Watch link card lives. transportation.export is NOT granted —
    # the Transfinder export button stays admin-only.
    ("transport", "Transport staff — Fleet Watch access via Nexus session, minimal Nexus visibility", [
        "dashboard.view",
        "transportation.view",
    ]),
]


async def seed_roles_and_permissions(db: AsyncSession):
    """Seed permissions and roles. Idempotent."""
    # Seed permissions
    for action, description, is_student_sensitive in PERMISSIONS:
        existing = await db.execute(select(Permission).where(Permission.action == action))
        if not existing.scalar_one_or_none():
            db.add(Permission(action=action, description=description, is_student_sensitive=is_student_sensitive))

    await db.commit()

    # Load all permissions into a lookup
    result = await db.execute(select(Permission))
    perm_lookup = {p.action: p.id for p in result.scalars().all()}

    # Seed roles and their permissions
    for role_name, description, perm_actions in ROLES:
        result = await db.execute(select(Role).where(Role.name == role_name))
        role = result.scalar_one_or_none()
        if not role:
            role = Role(name=role_name, description=description)
            db.add(role)
            await db.commit()
            await db.refresh(role)

        # Add missing permission links
        for action in perm_actions:
            perm_id = perm_lookup.get(action)
            if not perm_id:
                continue
            existing = await db.execute(
                select(RolePermission).where(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id == perm_id,
                )
            )
            if not existing.scalar_one_or_none():
                db.add(RolePermission(role_id=role.id, permission_id=perm_id))

    await db.commit()
    logger.info(f"Seeded {len(PERMISSIONS)} permissions and {len(ROLES)} roles")
