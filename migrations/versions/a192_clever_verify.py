"""Clever-vs-Nexus verification table + badge support.

Persists the result of a manual Clever CSV upload comparison against
current roster_snapshots. Each sis_id gets one row (upsert) — most
recent verification wins. Powers a per-student ✓ / ⚠ / unchecked
badge on the roster page.

One row per student — a re-upload overwrites the sis_id's row so the
badge always reflects the latest known state. If we ever need history,
add a separate roster_clever_verify_batches table; kept simple for now.

Revision ID: a192_clever_verify
Revises: a191_noc_services_monitoring
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a192_clever_verify"
down_revision = "a191_noc_services_monitoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roster_clever_verify",
        sa.Column("sis_id", sa.String(50), primary_key=True),
        sa.Column("matched", sa.Boolean, nullable=False),
        # List of {field, sis_value, roster_value} dicts. Empty list
        # when matched=True. Bounded to the compared field set so the
        # column stays small even for full-district uploads.
        sa.Column("mismatches", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("checked_by", sa.String(255), nullable=True),
        sa.Column("source_filename", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_roster_clever_verify_matched",
        "roster_clever_verify",
        ["matched"],
    )


def downgrade() -> None:
    op.drop_index("ix_roster_clever_verify_matched", table_name="roster_clever_verify")
    op.drop_table("roster_clever_verify")
