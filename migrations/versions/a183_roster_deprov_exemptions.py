"""Manual deprovision exemption list.

When a student has a non-district email AND no student_teachers rows,
they're a candidate for suspend + archive (they don't attend the district and
aren't in any district-scheduled class). BUT some of those kids are enrolled
in district alternative programs (credit recovery, vocational partnership,
etc.) that don't appear in the SIS Enrollments.csv. Those need to stay
active.

Since the alt-class enrollment isn't visible in any imported feed, the
operator has to tag those kids manually. This table holds those tags.
The nightly / manual deprovision sweep always respects entries here.

Revision ID: a183_roster_deprov_exemptions
Revises: a182_roster_google_ou
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a183_roster_deprov_exemptions"
down_revision = "a182_roster_google_ou"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roster_deprov_exemptions",
        sa.Column("sis_id", sa.String(64), primary_key=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("added_by", sa.String(255), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_roster_deprov_exemptions_added_at",
        "roster_deprov_exemptions", ["added_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_roster_deprov_exemptions_added_at", "roster_deprov_exemptions")
    op.drop_table("roster_deprov_exemptions")
