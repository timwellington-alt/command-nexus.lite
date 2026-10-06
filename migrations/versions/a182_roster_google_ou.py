"""Cache each student's current Google OU on roster_snapshots.

Powers the small OU chip on the student directory ("PHS", "PES",
"Archive"…) without having to hit the Google Directory API per row.
Snapshot-fresh: written by clever_import (via the same list_users
pull that already loads google_status_by_email) and by the reconcile
job's Direction A applier when it moves a kid's OU. Nullable so
students with no Google account carry NULL.

Revision ID: a182_roster_google_ou
Revises: a181_chromebook_swap_deployment
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a182_roster_google_ou"
down_revision = "a181_chromebook_swap_deployment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "roster_snapshots",
        sa.Column("google_ou", sa.String(200), nullable=True),
    )
    op.create_index(
        "ix_roster_snapshots_google_ou",
        "roster_snapshots", ["google_ou"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_roster_snapshots_google_ou", "roster_snapshots")
    op.drop_column("roster_snapshots", "google_ou")
