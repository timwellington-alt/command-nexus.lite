"""Add `transport` role for Fleet Watch sibling-app access.

Transport staff (bus drivers, dispatchers, transport director) need
to log into Nexus so Fleet Watch can read their `auth_groups` from
the Nexus session and gate access to dashcam footage on the
`nexus-transport-phs` Google group. They do NOT need operational
Nexus access — the role exists purely as a minimum viable identity
so the oauth callback doesn't deny their login with "no group
membership."

Grants only `dashboard.view` so they land on a working page in Nexus
instead of a 403. Everything else (staff directory, roster, access,
network, etc.) is off. Future transport-specific Nexus features can
attach permissions to this role as they land.

Pair with a `group_role_map` setting entry that maps
`nexus-transport-{building}@domain` → role="transport", scope="school",
scope_value="<building>". Without that setting update this migration
is functionally inert — the role exists but nobody gets assigned to
it at login.

Revision ID: a035_transport_role
Revises: a034_email_mismatch
Create Date: 2026-04-10
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a035_transport_role"
down_revision: Union[str, None] = "a034_email_mismatch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_ROLES = [
    ("transport",
     "Transport staff — Fleet Watch access via Nexus session, minimal Nexus visibility"),
]

# Permissions granted to the transport role. Deliberately minimal —
# just enough so the landing page renders instead of 403'ing.
TRANSPORT_PERMISSIONS = ["dashboard.view"]


def upgrade() -> None:
    conn = op.get_bind()

    role_table = sa.table(
        "roles",
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
    )
    rp_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission_id", sa.Integer),
    )

    # Insert the transport role (idempotent — skip if it already exists)
    existing = conn.execute(
        sa.text("SELECT id FROM roles WHERE name = 'transport'")
    ).fetchone()
    if not existing:
        for role_name, description in NEW_ROLES:
            conn.execute(role_table.insert().values(
                name=role_name, description=description,
            ))

    # Load permission IDs
    rows = conn.execute(sa.text("SELECT id, action FROM permissions"))
    perm_ids = {row[1]: row[0] for row in rows}

    # Assign permissions to transport role
    role_row = conn.execute(
        sa.text("SELECT id FROM roles WHERE name = 'transport'")
    ).fetchone()
    if role_row:
        transport_id = role_row[0]
        for action in TRANSPORT_PERMISSIONS:
            pid = perm_ids.get(action)
            if not pid:
                continue
            # Skip if link already exists (idempotent)
            existing = conn.execute(
                sa.text(
                    "SELECT 1 FROM role_permissions "
                    "WHERE role_id = :rid AND permission_id = :pid"
                ),
                {"rid": transport_id, "pid": pid},
            ).fetchone()
            if not existing:
                conn.execute(rp_table.insert().values(
                    role_id=transport_id, permission_id=pid,
                ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "DELETE FROM role_permissions WHERE role_id IN "
        "(SELECT id FROM roles WHERE name = 'transport')"
    ))
    conn.execute(sa.text("DELETE FROM roles WHERE name = 'transport'"))
