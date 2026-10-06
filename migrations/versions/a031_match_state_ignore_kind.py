"""Add match_state to staff_directory and kind to staff_ignores.

Migrates the deprovision detection logic from "diff against HR" to
"positive confirmation by HR or building roster". The staff_ignores
table gets a `kind` column distinguishing confirmed-overrides
(real staff we don't have an HR/roster source for) from non-person
trash (service accounts, shared mailboxes).

Also dismisses every pending deprovision queue entry produced by
the old logic, so the new sync starts from a clean slate.

Revision ID: a031_match_state
Revises: a030_paxton_dept
Create Date: 2026-04-09
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a031_match_state"
down_revision: Union[str, None] = "a030_paxton_dept"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── staff_ignores: add kind column ──
    # Existing rows were created when staff_ignores meant "exclude from
    # reconciliation reports" — that's the same as the new non_person
    # bucket, so default existing data to non_person.
    op.add_column(
        "staff_ignores",
        sa.Column("kind", sa.String(20), nullable=False, server_default="non_person"),
    )
    op.create_index("ix_staff_ignores_kind", "staff_ignores", ["kind"])

    # ── staff_directory: add match_state column ──
    op.add_column(
        "staff_directory",
        sa.Column("match_state", sa.String(20), nullable=True),
    )
    op.create_index("ix_staff_directory_match_state", "staff_directory", ["match_state"])

    # ── staff_reconciliation: mirror the same column so the directory
    #    UI can filter without joining staff_directory. ──
    op.add_column(
        "staff_reconciliation",
        sa.Column("match_state", sa.String(20), nullable=True),
    )
    op.create_index("ix_staff_reconciliation_match_state", "staff_reconciliation", ["match_state"])

    # ── Bulk-dismiss zombie deprovisions from old logic ──
    op.execute("""
        UPDATE staff_queue
        SET status = 'dismissed',
            completed_at = NOW(),
            error = COALESCE(error, '') || ' [auto-dismissed by a031 migration: old detection logic retired]'
        WHERE action = 'deprovision'
          AND status IN ('pending_data', 'pending', 'ready')
    """)


def downgrade() -> None:
    op.drop_index("ix_staff_reconciliation_match_state", table_name="staff_reconciliation")
    op.drop_column("staff_reconciliation", "match_state")
    op.drop_index("ix_staff_directory_match_state", table_name="staff_directory")
    op.drop_column("staff_directory", "match_state")
    op.drop_index("ix_staff_ignores_kind", table_name="staff_ignores")
    op.drop_column("staff_ignores", "kind")
