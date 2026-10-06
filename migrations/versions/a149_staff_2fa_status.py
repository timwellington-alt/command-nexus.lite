"""2FA (2SV) enrollment + enforcement status on staff.

Google Directory API exposes `isEnrolledIn2Sv` (user has set up 2SV)
and `isEnforcedIn2Sv` (admin has required them to). The staff sync
already fetches `projection=full` so both fields are in the response
— this migration adds the columns to persist them and mirror to
staff_reconciliation for the directory read model.

Both columns are nullable (as opposed to `default=false`) so we can
tell "we haven't fetched yet for this user" apart from "user is
definitely not enrolled" — the difference matters for a fresh install
before the first staff sync completes, and for any user Google's API
declines to report on.

Revision ID: a149_staff_2fa_status
Revises: a148_chromebook_ou_history
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a149_staff_2fa_status"
down_revision = "a148_chromebook_ou_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("staff_directory",
                  sa.Column("is_enrolled_in_2sv", sa.Boolean, nullable=True))
    op.add_column("staff_directory",
                  sa.Column("is_enforced_in_2sv", sa.Boolean, nullable=True))
    op.add_column("staff_reconciliation",
                  sa.Column("is_enrolled_in_2sv", sa.Boolean, nullable=True))
    op.add_column("staff_reconciliation",
                  sa.Column("is_enforced_in_2sv", sa.Boolean, nullable=True))
    # Partial index for "who needs to enroll" — the most common query
    # pattern (skips the ~majority who ARE enrolled). Nulls also
    # match, so users we haven't fetched yet show up as "unknown"
    # in the same query.
    op.create_index(
        "ix_staff_directory_needs_2sv",
        "staff_directory",
        ["email"],
        postgresql_where=sa.text("is_enrolled_in_2sv IS NOT TRUE"),
    )


def downgrade() -> None:
    op.drop_index("ix_staff_directory_needs_2sv", "staff_directory")
    op.drop_column("staff_reconciliation", "is_enforced_in_2sv")
    op.drop_column("staff_reconciliation", "is_enrolled_in_2sv")
    op.drop_column("staff_directory", "is_enforced_in_2sv")
    op.drop_column("staff_directory", "is_enrolled_in_2sv")
