"""Tickets — Chromebook repair depot side table.

Backs the new repair-depot workflow. Every repair ticket
(``category='technology'``, ``sub_category='chromebook_repair'``) gets
a 1:1 side row capturing depot-specific state:

  - device identity (serial + asset_tag snapshot — survives re-imaging)
  - multi-component failure categories (JSON list)
  - depot lifecycle state independent of the main ticket status
  - recent_users snapshot at intake (sliding-5 window for the
    destructive-tendency report — see SPEC §X)
  - optional loaner-out ledger
  - parts consumed + labor + disposition
  - witness-confirmed culprit (overrides the inferential watchlist)

Bulk drop-offs from the /chromebooks bulk-bar create rows with
``depot_status='pending_intake'``; the intake form fills the rest
later. Cross-ticket history is keyed on chromebook_serial.

Revision ID: a106_chromebook_repair_depot
Revises: a105_schedule_invitees
Create Date: 2026-05-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a106_chromebook_repair_depot"
down_revision: Union[str, None] = "a105_schedule_invitees"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ticket_chromebook_repair",
        sa.Column("ticket_id", sa.Integer(),
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"),
                  primary_key=True),
        # Device identity — snapshotted so a re-imaged or re-asset-tagged
        # CB doesn't lose its repair history.
        sa.Column("chromebook_serial", sa.String(255), nullable=False, index=True),
        sa.Column("chromebook_asset_tag", sa.String(50)),
        sa.Column("chromebook_model", sa.String(255)),
        # Intake state
        sa.Column("intake_at", sa.DateTime(timezone=True)),
        sa.Column("received_from_email", sa.String(255)),
        sa.Column("on_behalf_of_student", sa.String(255)),
        # JSON list of failure-category codes (multi-component).
        # Validation list lives in policy.py; column stays free-form so
        # operators can extend without a migration.
        sa.Column("failure_categories", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("reported_issue", sa.Text()),
        sa.Column("diagnosed_issue", sa.Text()),
        # Depot lifecycle — independent of tickets.status. Allows pending
        # intake to be a real state (no diagnosis yet) without confusing
        # the standard submitted→assigned→in_progress flow.
        sa.Column("depot_status", sa.String(30), nullable=False, server_default="pending_intake"),
        # Parts + labor
        sa.Column("parts_used", sa.Text(), nullable=False, server_default="[]"),  # JSON [{item_id, qty}]
        sa.Column("labor_minutes", sa.Integer()),
        # Warranty — manual flag for Phase 1; manufacturer API lookup is Phase 3
        sa.Column("warranty_claim_filed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("warranty_provider", sa.String(100)),
        # Loaner ledger — optional; many classrooms use cart spares so
        # this won't be filled for most repairs. NULL == no formal loaner.
        sa.Column("loaner_chromebook_serial", sa.String(255)),
        sa.Column("loaner_issued_at", sa.DateTime(timezone=True)),
        sa.Column("loaner_returned_at", sa.DateTime(timezone=True)),
        # Destructive-tendency analysis
        # JSON list: [{email, last_seen_at}, ...] — sliding-5 window
        # captured at intake. NOT recomputed later (so report data
        # doesn't drift as the device rotates through other users).
        sa.Column("recent_users", sa.Text(), nullable=False, server_default="[]"),
        # When a teacher witnesses the damage in person, the culprit is
        # known directly — overrides the inferential watchlist.
        sa.Column("witness_culprit_email", sa.String(255)),
        sa.Column("witness_notes", sa.Text()),
        # Disposition
        sa.Column("disposition", sa.String(30)),  # repaired | beyond_repair | warranty_replace
        sa.Column("returned_at", sa.DateTime(timezone=True)),
        sa.Column("returned_to_email", sa.String(255)),
        sa.CheckConstraint(
            "depot_status IN ("
            "'pending_intake','received','diagnosed','parts_ordered',"
            "'in_repair','ready_return','returned','beyond_repair')",
            name="ck_ticket_chromebook_repair_depot_status",
        ),
    )
    op.create_index(
        "ix_ticket_chromebook_repair_depot_status",
        "ticket_chromebook_repair", ["depot_status"],
    )

    # Seed the new dispatch permission so the repair-depot list page +
    # bulk send-to-depot button can be gated cleanly. Tech-dispatch
    # users get it automatically.
    op.execute(
        "INSERT INTO permissions (action, description) "
        "VALUES ('tickets.chromebook_repair.dispatch', "
        "'Manage the Chromebook repair depot — intake, status changes, returns') "
        "ON CONFLICT (action) DO NOTHING"
    )
    op.execute(
        "INSERT INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
        "WHERE r.name IN ('admin') AND p.action = 'tickets.chromebook_repair.dispatch' "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_index("ix_ticket_chromebook_repair_depot_status",
                  table_name="ticket_chromebook_repair")
    op.drop_table("ticket_chromebook_repair")
    op.execute(
        "DELETE FROM permissions WHERE action='tickets.chromebook_repair.dispatch'"
    )
