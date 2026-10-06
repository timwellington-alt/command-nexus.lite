"""Tickets module — Phase A foundation.

Creates the base ``tickets`` table + three category side-tables
(``ticket_maintenance``, ``ticket_technology``, ``ticket_transportation``)
+ ``ticket_comments`` + ``ticket_attachments``. Seeds the new
``tickets.*`` permissions and grants them to existing roles where
appropriate (admin gets all; per-category roles get scoped subsets).

See ``Project Specs/tickets/TICKETS_MODULE_SPEC.md`` §4 for the data
model rationale; this migration implements it 1:1.

The ``status``/``category``/``sub_category``/``priority`` columns use
plain strings with DB CHECK constraints (same pattern as inventory,
door_events). Avoids enum migrations every time we add a value.

Revision ID: a098_tickets_module
Revises: a097_user_preferences
Create Date: 2026-05-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a098_tickets_module"
down_revision: Union[str, None] = "a097_user_preferences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Allowed value sets (kept here so the seed below + the model can
#     reference the same source of truth) ─────────────────────────────

VALID_CATEGORIES = ("maintenance", "technology", "transportation")
VALID_STATUSES = (
    "submitted", "awaiting_approval", "approved", "assigned",
    "in_progress", "on_hold", "ready_pickup",
    "closed_complete", "closed_cancelled", "closed_beyond_repair",
)
VALID_PRIORITIES = ("low", "normal", "high", "urgent")


def upgrade() -> None:
    # ── tickets (base) ────────────────────────────────────────────────
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("sub_category", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("requester_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("assignee_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("building", sa.String(20), nullable=True),
        sa.Column("floor", sa.Integer, nullable=True),
        sa.Column("room", sa.String(100), nullable=True),
        sa.Column("area", sa.String(50), nullable=True),
        # facility_asset_id FK is created without a real FK constraint
        # right now because the facility_assets table isn't built until
        # Phase B. Column reserved for forward-compat; CHECK enforces
        # nullable. Phase B migration adds the FK.
        sa.Column("facility_asset_id", sa.Integer, nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="submitted"),
        sa.Column("priority", sa.String(10), nullable=False, server_default="normal"),
        sa.Column("sla_target_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.Text, nullable=True),
        sa.Column("parent_ticket_id", sa.Integer,
                  sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "category IN (" + ",".join(f"'{c}'" for c in VALID_CATEGORIES) + ")",
            name="tickets_category_check",
        ),
        sa.CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in VALID_STATUSES) + ")",
            name="tickets_status_check",
        ),
        sa.CheckConstraint(
            "priority IN (" + ",".join(f"'{p}'" for p in VALID_PRIORITIES) + ")",
            name="tickets_priority_check",
        ),
        sa.CheckConstraint(
            "(closed_at IS NULL) = (status NOT LIKE 'closed_%')",
            name="tickets_closed_at_consistency",
        ),
    )
    # Indexes for common query paths
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_category_sub", "tickets", ["category", "sub_category"])
    op.create_index("ix_tickets_assignee", "tickets", ["assignee_user_id"])
    op.create_index("ix_tickets_requester", "tickets", ["requester_user_id"])
    op.create_index("ix_tickets_building", "tickets", ["building"])
    op.create_index("ix_tickets_opened", "tickets", ["opened_at"])
    op.create_index(
        "ix_tickets_open", "tickets", ["category", "status"],
        postgresql_where=sa.text("status NOT LIKE 'closed_%'"),
    )

    # ── ticket_maintenance ──────────────────────────────────────────
    op.create_table(
        "ticket_maintenance",
        sa.Column("ticket_id", sa.Integer,
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("safety_issue", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("affected_area", sa.String(30), nullable=True),
    )

    # ── ticket_technology ───────────────────────────────────────────
    op.create_table(
        "ticket_technology",
        sa.Column("ticket_id", sa.Integer,
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("device_asset_tag", sa.String(50), nullable=True),
        sa.Column("device_serial", sa.String(255), nullable=True),
        sa.Column("chromebook_serial", sa.String(255), nullable=True),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("auto_triage_notes", sa.Text, nullable=True),
    )
    op.create_index("ix_ticket_technology_cb_serial",
                    "ticket_technology", ["chromebook_serial"])

    # ── ticket_transportation ───────────────────────────────────────
    op.create_table(
        "ticket_transportation",
        sa.Column("ticket_id", sa.Integer,
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True),
        # field_trip fields
        sa.Column("trip_destination", sa.String(255), nullable=True),
        sa.Column("travel_date", sa.Date, nullable=True),
        sa.Column("departure_time", sa.Time, nullable=True),
        sa.Column("return_time", sa.Time, nullable=True),
        sa.Column("headcount", sa.Integer, nullable=True),
        sa.Column("purpose", sa.Text, nullable=True),
        sa.Column("budget_code", sa.String(50), nullable=True),
        # approval
        sa.Column("approver_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.Text, nullable=True),
        # bus_maintenance fields
        sa.Column("bus_number", sa.String(20), nullable=True),
        sa.Column("mileage", sa.Integer, nullable=True),
        sa.Column("symptom", sa.Text, nullable=True),
    )

    # ── ticket_comments ─────────────────────────────────────────────
    op.create_table(
        "ticket_comments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.Integer,
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("is_internal", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_ticket_comments_ticket", "ticket_comments", ["ticket_id"])

    # ── ticket_attachments ──────────────────────────────────────────
    op.create_table(
        "ticket_attachments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.Integer,
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=True),
        sa.Column("comment_id", sa.Integer,
                  sa.ForeignKey("ticket_comments.id", ondelete="CASCADE"), nullable=True),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("file_mime", sa.String(50), nullable=False),
        sa.Column("file_size_bytes", sa.Integer, nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("captured_by_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "(ticket_id IS NOT NULL) OR (comment_id IS NOT NULL)",
            name="ticket_attachments_parent_check",
        ),
        sa.UniqueConstraint("file_sha256", "ticket_id", "comment_id",
                            name="ticket_attachments_dedup"),
    )
    op.create_index("ix_ticket_attachments_ticket", "ticket_attachments", ["ticket_id"])
    op.create_index("ix_ticket_attachments_comment", "ticket_attachments", ["comment_id"])

    # ── Seed permissions ────────────────────────────────────────────
    perms = [
        ("tickets.view",                              "View own tickets and tickets assigned to me"),
        ("tickets.submit",                            "Submit a new ticket"),
        ("tickets.admin",                             "Full ticket admin — see/edit all"),
        ("tickets.maintenance.dispatch",              "Assign Maintenance tickets (non-custodial)"),
        ("tickets.maintenance.custodial.dispatch",    "Assign Custodial Maintenance tickets (scoped by building)"),
        ("tickets.technology.claim",                  "Self-claim Technology tickets"),
        ("tickets.technology.password_reset",         "Execute automated password reset on Technology tickets"),
        ("tickets.transportation.approve",            "Approve/reject Transportation tickets"),
        ("tickets.transportation.dispatch",           "Assign Transportation tickets after approval"),
    ]
    op.execute("CREATE TEMP TABLE _tickets_perm_seed (action TEXT, description TEXT)")
    for a, d in perms:
        op.execute(sa.text(
            "INSERT INTO _tickets_perm_seed (action, description) VALUES (:a, :d)"
        ).bindparams(a=a, d=d))
    op.execute(
        "INSERT INTO permissions (action, description, is_student_sensitive) "
        "SELECT action, description, false FROM _tickets_perm_seed "
        "WHERE action NOT IN (SELECT action FROM permissions)"
    )

    # Grant ALL the tickets perms to the admin role.
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id "
        "FROM roles r CROSS JOIN permissions p "
        "WHERE r.name = 'admin' AND p.action LIKE 'tickets.%' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM role_permissions rp "
        "    WHERE rp.role_id = r.id AND rp.permission_id = p.id"
        "  )"
    )
    # Any-staff baseline: tickets.view + tickets.submit go to the
    # 'staff' role so everyone with a district account can file and see
    # their own. (Other ticket perms remain admin-only until roles are
    # explicitly mapped by an operator in Settings → Roles.)
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id "
        "FROM roles r CROSS JOIN permissions p "
        "WHERE r.name = 'staff' AND p.action IN ('tickets.view', 'tickets.submit') "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM role_permissions rp "
        "    WHERE rp.role_id = r.id AND rp.permission_id = p.id"
        "  )"
    )


def downgrade() -> None:
    # Remove permission grants + permissions
    op.execute("DELETE FROM role_permissions WHERE permission_id IN "
               "(SELECT id FROM permissions WHERE action LIKE 'tickets.%')")
    op.execute("DELETE FROM permissions WHERE action LIKE 'tickets.%'")
    # Drop tables in reverse dependency order
    op.drop_table("ticket_attachments")
    op.drop_table("ticket_comments")
    op.drop_table("ticket_transportation")
    op.drop_table("ticket_technology")
    op.drop_table("ticket_maintenance")
    op.drop_table("tickets")
