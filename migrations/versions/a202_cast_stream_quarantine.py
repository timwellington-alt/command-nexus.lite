"""Cast-stream quarantine + wider status column.

Adds `consecutive_failures` counter so the keepalive job can quarantine
a Chromecast that has failed N times in a row instead of retrying every
60s forever (which was leaking FDs and killing the worker on 2026-09-17).
Also widens `last_status` from varchar(30) to varchar(80) so longer
diagnostic strings fit (previous cap silently truncated a manual
disable message during triage).

Revision ID: a202_cast_stream_quarantine
Revises: a201_student_membership
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a202_cast_stream_quarantine"
down_revision = "a201_student_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cast_persistent_streams",
        sa.Column(
            "consecutive_failures", sa.Integer, nullable=False, server_default="0"
        ),
    )
    op.alter_column(
        "cast_persistent_streams", "last_status",
        type_=sa.String(80), existing_nullable=False,
        existing_server_default=sa.text("'pending'::character varying"),
    )


def downgrade() -> None:
    op.alter_column(
        "cast_persistent_streams", "last_status",
        type_=sa.String(30), existing_nullable=False,
    )
    op.drop_column("cast_persistent_streams", "consecutive_failures")
