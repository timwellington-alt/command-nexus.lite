"""Create staff_queue table.

Historically in the full nexus codebase this table was materialized by
Base.metadata.create_all() at app startup, not by a migration. Lite
forks that run on a cleanly-seeded postgres with alembic-only schema
management hit UndefinedTable when a028 tries to ALTER it.

This migration slots between a027_staff_reconciliation and
a028_queue so the subsequent ALTER migrations have a target. Only
the base columns the raw-SQL INSERTs in hr_diff_job + onboarding
need pre-a028 go here; everything else is layered on by a028+.

Revision ID: a027a_staff_queue_create
Revises: a027_staff_reconciliation
Create Date: 2026-10-07
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a027a_staff_queue_create"
down_revision: Union[str, None] = "a027_staff_reconciliation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_queue",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("first_name", sa.String(100)),
        sa.Column("last_name", sa.String(100)),
        sa.Column("email", sa.String(255)),
        sa.Column("building", sa.String(100)),
        sa.Column("role_type", sa.String(50)),
        sa.Column("title", sa.String(255)),
        sa.Column("details", sa.Text()),
        sa.Column("source", sa.String(50)),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_staff_queue_status", "staff_queue", ["status"])
    op.create_index("ix_staff_queue_email", "staff_queue", ["email"])


def downgrade() -> None:
    op.drop_index("ix_staff_queue_email", table_name="staff_queue")
    op.drop_index("ix_staff_queue_status", table_name="staff_queue")
    op.drop_table("staff_queue")
