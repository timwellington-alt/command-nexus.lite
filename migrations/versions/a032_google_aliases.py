"""Store Google aliases on staff_directory for HR diff matching.

The HR sync auto-queue was producing false-positive provision entries
for people whose Google account uses a different last name than HR
(e.g. Jennifer Buffington primary + jennifer.carver alias, HR lists
her under "Carver"). The staff_sync job already reads aliases and uses
them for its own match_state decision, but doesn't persist them —
so the downstream HR-diff loop re-evaluated each HR row against
primary emails only and couldn't see aliases.

Adds `google_aliases` as JSON-encoded TEXT (list of lowercased
alias emails).

Revision ID: a032_google_aliases
Revises: a031_match_state
Create Date: 2026-04-09
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a032_google_aliases"
down_revision: Union[str, None] = "a031_match_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "staff_directory",
        sa.Column("google_aliases", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("staff_directory", "google_aliases")
