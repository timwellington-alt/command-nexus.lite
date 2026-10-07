"""Grant `transportation.view` to the `transport` role.

Initial `a035_transport_role` only gave the new role `dashboard.view`.
That left the Transport nav link hidden (every page's context builder
gates it on `transportation.view`) and the `/transportation` page
itself inaccessible (its route uses `require_action("transportation.view")`).

Since the Fleet Watch link card lives on the Transportation page,
transport staff who have the role still couldn't discover or reach
the sibling app. Add the missing permission link here as an
idempotent additive change.

Transfinder export actions are separately gated by
`transportation.export`, which is deliberately NOT granted to the
transport role — transport staff can see the page and the Fleet
Watch card, but cannot run the Transfinder export. That stays with
admin / operations.

Revision ID: a036_transport_view
Revises: a035_transport_role
Create Date: 2026-04-10
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a036_transport_view"
down_revision: Union[str, None] = "a035_transport_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TRANSPORT_ROLE = "transport"
NEW_PERMISSION = "transportation.view"


def upgrade() -> None:
    conn = op.get_bind()

    role_row = conn.execute(
        sa.text("SELECT id FROM roles WHERE name = :n"),
        {"n": TRANSPORT_ROLE},
    ).fetchone()
    if not role_row:
        # If the role isn't there, a035 didn't run — nothing to do.
        return
    role_id = role_row[0]

    perm_row = conn.execute(
        sa.text("SELECT id FROM permissions WHERE action = :a"),
        {"a": NEW_PERMISSION},
    ).fetchone()
    if not perm_row:
        # Permission doesn't exist. In the full nexus codebase this
        # would be a deploy problem — but in forks that strip the
        # transportation module (nexus-lite), the permission is
        # genuinely absent and that's fine. Skip the grant.
        return
    perm_id = perm_row[0]

    existing = conn.execute(
        sa.text(
            "SELECT 1 FROM role_permissions "
            "WHERE role_id = :rid AND permission_id = :pid"
        ),
        {"rid": role_id, "pid": perm_id},
    ).fetchone()
    if existing:
        return  # Idempotent — already linked

    conn.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "VALUES (:rid, :pid)"
        ),
        {"rid": role_id, "pid": perm_id},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "DELETE FROM role_permissions WHERE "
        "role_id = (SELECT id FROM roles WHERE name = :rn) AND "
        "permission_id = (SELECT id FROM permissions WHERE action = :pa)"
    ), {"rn": TRANSPORT_ROLE, "pa": NEW_PERMISSION})
