"""Seed ticket + inventory operator roles.

Until now only `admin` carried any tickets.* or inventory.* perms.
This adds the missing operator roles so the Group → Role mapping UI
has real targets for routing dispatchers, approvers, and inventory
staff. Existing custom roles (anything not in the seed list) are
left alone.

Roles added (all idempotent — ON CONFLICT DO NOTHING):

  Tickets
    requester                 view + submit + dashboard
    maintenance_dispatcher    view + maintenance.dispatch
    custodial_dispatcher      view + maintenance.custodial.dispatch
                              (typically used building-scoped via
                               role_mappings scope='school')
    tech_dispatcher           view + technology.claim
                                   + technology.password_reset
                                   + chromebook_repair.dispatch
    transportation_approver   view + transportation.approve
    transportation_dispatcher view + transportation.dispatch
    schedule_approver         view + schedule.approve

  Inventory
    inventory_viewer          inventory.view + dashboard
    inventory_admin           full inventory.* (view, edit, checkout,
                              labels, audit, enrich, categories,
                              orders.view/receive/manage)

Note: `tickets.view` is required for ALL ticket roles — the list
endpoints gate on it before the visibility-tier filter narrows down
what they see. Dispatchers without it would 403 on /api/tickets.

Revision ID: a108_ticket_facility_roles
Revises: a107_voice_quiet_weekends
Create Date: 2026-05-16
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a108_ticket_facility_roles"
down_revision: Union[str, None] = "a107_voice_quiet_weekends"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (role_name, description, [permission actions])
NEW_ROLES: list[tuple[str, str, list[str]]] = [
    ("requester",
     "Baseline staff — submit tickets and see your own",
     ["tickets.view", "tickets.submit", "dashboard.view"]),
    ("maintenance_dispatcher",
     "Dispatches non-custodial Maintenance tickets",
     ["tickets.view", "tickets.maintenance.dispatch"]),
    ("custodial_dispatcher",
     "Dispatches Custodial Maintenance tickets (usually building-scoped)",
     ["tickets.view", "tickets.maintenance.custodial.dispatch"]),
    ("tech_dispatcher",
     "Claims Technology tickets including Chromebook repair intake",
     ["tickets.view",
      "tickets.technology.claim",
      "tickets.technology.password_reset",
      "tickets.chromebook_repair.dispatch"]),
    ("transportation_approver",
     "Approves/rejects Transportation tickets",
     ["tickets.view", "tickets.transportation.approve"]),
    ("transportation_dispatcher",
     "Assigns Transportation tickets after approval",
     ["tickets.view", "tickets.transportation.dispatch"]),
    ("schedule_approver",
     "Approves facility-use Schedule tickets",
     ["tickets.view", "tickets.schedule.approve"]),
    ("inventory_viewer",
     "Inventory read-only access",
     ["inventory.view", "dashboard.view"]),
    ("inventory_admin",
     "Full inventory management — items, checkouts, audits, orders",
     ["inventory.view", "inventory.edit", "inventory.checkout.manage",
      "inventory.labels.print", "inventory.audit.run",
      "inventory.enrich", "inventory.categories.manage",
      "inventory.orders.view", "inventory.orders.receive",
      "inventory.orders.manage"]),
]


def upgrade() -> None:
    for name, description, perms in NEW_ROLES:
        op.execute(
            "INSERT INTO roles (name, description) "
            f"VALUES ('{name}', '{description.replace(chr(39), chr(39)*2)}') "
            "ON CONFLICT (name) DO NOTHING"
        )
        # Grant each listed permission. Skips silently if either the
        # permission row is missing or the link already exists.
        for action in perms:
            op.execute(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT r.id, p.id "
                "FROM roles r CROSS JOIN permissions p "
                f"WHERE r.name = '{name}' AND p.action = '{action}' "
                "ON CONFLICT (role_id, permission_id) DO NOTHING"
            )


def downgrade() -> None:
    names = ", ".join(f"'{n}'" for n, _, _ in NEW_ROLES)
    op.execute(
        f"DELETE FROM role_permissions WHERE role_id IN "
        f"(SELECT id FROM roles WHERE name IN ({names}))"
    )
    op.execute(f"DELETE FROM roles WHERE name IN ({names})")
