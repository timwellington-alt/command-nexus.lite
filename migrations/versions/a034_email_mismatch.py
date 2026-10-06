"""Flag staff whose Google primary email is out of date vs HR.

HR's Notes column carries name-change signals like:

    "Née: Buffington"
    "Formerly: Brewer"
    "New Née: Malone"

When Nexus sees a Google account whose local-part still uses the
maiden name but HR has the person under their current legal name,
the account matches (via alias or the Née variant added to the
matcher) but the primary email is stale. We flag these so IT can
review and rename in one place instead of noticing ad-hoc when
reception asks "why is Jennifer Carver getting mail to Buffington?"

Adds:

  - ``staff_directory.email_mismatch``      — derived bool, true when
    the HR-matched row's Google username local-part doesn't match
    ``<first>.<last>`` based on HR's current name.
  - ``staff_reconciliation.email_mismatch`` — mirrored for the
    directory endpoint's single-query read path.

Revision ID: a034_email_mismatch
Revises: a033_hr_notes_loa
Create Date: 2026-04-09
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a034_email_mismatch"
down_revision: Union[str, None] = "a033_hr_notes_loa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "staff_directory",
        sa.Column(
            "email_mismatch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "staff_reconciliation",
        sa.Column(
            "email_mismatch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_staff_directory_email_mismatch",
        "staff_directory",
        ["email_mismatch"],
    )
    op.create_index(
        "ix_staff_reconciliation_email_mismatch",
        "staff_reconciliation",
        ["email_mismatch"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_staff_reconciliation_email_mismatch",
        table_name="staff_reconciliation",
    )
    op.drop_index(
        "ix_staff_directory_email_mismatch",
        table_name="staff_directory",
    )
    op.drop_column("staff_reconciliation", "email_mismatch")
    op.drop_column("staff_directory", "email_mismatch")
