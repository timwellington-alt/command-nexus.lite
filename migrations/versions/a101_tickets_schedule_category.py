"""Tickets module — schedule category + ticket_schedule side table.

Schedule is the 4th top-level category (after maintenance, technology,
transportation). It models a facility-use request (event reservation):
who, where, when, for what.

Lifecycle mirrors transportation's: a new schedule ticket lands in
``awaiting_approval`` and routes to the configured facility-use
coordinator (Settings ▸ Tickets ▸ schedule_approver). On approve, the
service layer spawns child tickets in custodial/maintenance/technology
for each ``services_needed`` entry, each linked via parent_ticket_id.

Recurrence is deferred to Phase 2 — Phase 1 ships single-occurrence
events only. The side-table omits rrule columns to keep the door open
without locking a model decision.

Revision ID: a101_tickets_schedule_category
Revises: a100_facility_rooms_reservable
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a101_tickets_schedule_category"
down_revision: Union[str, None] = "a100_facility_rooms_reservable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ticket_schedule",
        sa.Column("ticket_id", sa.Integer(),
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day",  sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("setup_minutes",    sa.Integer(), nullable=True),
        sa.Column("teardown_minutes", sa.Integer(), nullable=True),
        sa.Column("attendee_count",   sa.Integer(), nullable=True),
        # JSON-as-text list of service categories to spawn on approval.
        # Values from {"custodial", "maintenance", "technology"}.
        sa.Column("services_needed", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_by_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("end_at >= start_at", name="ticket_schedule_time_order"),
    )
    op.create_index(
        "ix_ticket_schedule_start", "ticket_schedule", ["start_at"],
    )

    # Widen tickets.category CHECK to include 'schedule'.
    op.drop_constraint("tickets_category_check", "tickets", type_="check")
    op.create_check_constraint(
        "tickets_category_check", "tickets",
        "category IN ('maintenance','technology','transportation','schedule')",
    )

    # Seed the schedule_approver setting key so it appears in the UI
    # immediately after deploy (value empty until configured).
    op.execute(
        "INSERT INTO integration_configs (integration, key, value, is_secret_ref) "
        "VALUES ('tickets', 'schedule_approver', '', false) "
        "ON CONFLICT (integration, key) DO NOTHING"
    )
    # Seed the schedule-approve permission + grant to admin role.
    op.execute(
        "INSERT INTO permissions (action, description) "
        "VALUES ('tickets.schedule.approve', "
        "'Approve or reject facility-use (schedule) requests') "
        "ON CONFLICT (action) DO NOTHING"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
        "WHERE r.name = 'admin' AND p.action = 'tickets.schedule.approve' "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_constraint("tickets_category_check", "tickets", type_="check")
    op.create_check_constraint(
        "tickets_category_check", "tickets",
        "category IN ('maintenance','technology','transportation')",
    )
    op.drop_index("ix_ticket_schedule_start", table_name="ticket_schedule")
    op.drop_table("ticket_schedule")
    op.execute(
        "DELETE FROM integration_configs "
        "WHERE integration='tickets' AND key='schedule_approver'"
    )
