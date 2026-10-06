"""Local email override for students where Clever/SIS has the wrong
email. The Clever import consults this table BEFORE trusting
Student_email, so a bad SIS row can't keep re-tagging a properly-
provisioned account as ``wrong_format`` on every daily import.

Real-world trigger (2026-08-24): Logan Lester (sid=407548395) has
SIS email `lestera37@` (typo) but the correct + active Google account
is `lesterl37@`. Without this, the daily import would keep tagging
Logan and mis-status his account status until SIS is corrected.

Revision ID: a177_roster_email_overrides
Revises: a176_roster_provisioning_runs
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a177_roster_email_overrides"
down_revision = "a176_roster_provisioning_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roster_email_overrides",
        sa.Column("sis_id", sa.String(64), primary_key=True),
        sa.Column("override_email", sa.String(320), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("roster_email_overrides")
