"""Track HR Notes column + derived LOA state.

HR's master spreadsheet uses column B ("Notes") as a free-text field
that holds:

  - LOA indicators: "On LOA", "On Admin Leave", "On FMLA",
    "On Maternity Leave", "On Sick Leave", etc.
  - New-hire flag: "New"
  - Name-change signals: "Née: Maiden", "Formerly: Previous"

The Nexus HR sync has been ignoring this column entirely, which meant:

  1. LOA staff were being auto-queued for provision every time HR
     sync ran (they look like new hires because they don't have a
     Google account yet but ARE in HR).
  2. The staff directory couldn't distinguish "suspended because LOA"
     from "suspended because in-progress deprovision".
  3. Reception had no way to see why an account is suspended or
     when to expect the person back.

This migration adds:

  - ``hr_staff_cache.notes``              — raw free text from the
    HR Notes column. Source of truth.
  - ``staff_directory.hr_notes``          — copied by staff_sync_job
    when HR matches, so directory queries don't need a second join.
  - ``staff_directory.on_leave``          — derived boolean, true
    when notes match any leave keyword (LOA, FMLA, etc.).
  - ``staff_reconciliation.hr_notes``     — mirrors staff_directory
    so the directory endpoint (which reads reconciliation) can
    surface the LOA badge without another join.
  - ``staff_reconciliation.on_leave``     — same.

Revision ID: a033_hr_notes_loa
Revises: a032_google_aliases
Create Date: 2026-04-09
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a033_hr_notes_loa"
down_revision: Union[str, None] = "a032_google_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hr_staff_cache",
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "staff_directory",
        sa.Column("hr_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "staff_directory",
        sa.Column("on_leave", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "staff_reconciliation",
        sa.Column("hr_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "staff_reconciliation",
        sa.Column("on_leave", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_staff_directory_on_leave", "staff_directory", ["on_leave"])
    op.create_index("ix_staff_reconciliation_on_leave", "staff_reconciliation", ["on_leave"])


def downgrade() -> None:
    op.drop_index("ix_staff_reconciliation_on_leave", table_name="staff_reconciliation")
    op.drop_index("ix_staff_directory_on_leave", table_name="staff_directory")
    op.drop_column("staff_reconciliation", "on_leave")
    op.drop_column("staff_reconciliation", "hr_notes")
    op.drop_column("staff_directory", "on_leave")
    op.drop_column("staff_directory", "hr_notes")
    op.drop_column("hr_staff_cache", "notes")
