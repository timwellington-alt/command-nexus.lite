"""Ohio teacher certification number cached from HR master.

HR's MASTER tab has column N (`CERT #`) holding the state teacher
license/registration number for anyone classified `Cert`. Persisted so
teacher profiles can display it without a live SMB round-trip.

Only teachers have this populated. Nullable — non-cert staff have no
value, and any tab without a cert-column config leaves it empty.

Revision ID: a150_hr_cert_number
Revises: a149_staff_2fa_status
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a150_hr_cert_number"
down_revision = "a149_staff_2fa_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hr_staff_cache",
        sa.Column("cert_number", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hr_staff_cache", "cert_number")
