"""Add roster_changes, google_change_log, and email_ignores tables.

Revision ID: a016_roster_extras
Revises: a015_network_extras
Create Date: 2026-03-29
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a016_roster_extras"
down_revision: Union[str, None] = "a015_network_extras"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── roster_changes ─────────────────────────────────────────────────
    op.create_table(
        "roster_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "import_id",
            sa.Integer(),
            sa.ForeignKey("roster_imports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("student_id", sa.String(50), nullable=True),
        sa.Column("change_type", sa.String(50), nullable=False),
        sa.Column("school_code", sa.String(50), nullable=True),
        sa.Column("student_name", sa.String(255), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("google_provisioned", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("nutrikids_queued", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("nutrikids_sent", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_roster_changes_student_id", "roster_changes", ["student_id"])
    op.create_index("ix_roster_changes_change_type", "roster_changes", ["change_type"])
    op.create_index("ix_roster_changes_import_id", "roster_changes", ["import_id"])
    op.create_index("ix_roster_changes_created_at", "roster_changes", ["created_at"])

    # ── google_change_log ──────────────────────────────────────────────
    op.create_table(
        "google_change_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("target_email", sa.String(255), nullable=False),
        sa.Column("student_id", sa.String(50), nullable=True),
        sa.Column("before_state", sa.Text(), nullable=True),
        sa.Column("after_state", sa.Text(), nullable=True),
        sa.Column("reverted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverted_by", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_google_change_log_changed_at", "google_change_log", ["changed_at"])
    op.create_index("ix_google_change_log_action", "google_change_log", ["action"])
    op.create_index("ix_google_change_log_target_email", "google_change_log", ["target_email"])

    # ── email_ignores ──────────────────────────────────────────────────
    op.create_table(
        "email_ignores",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ignored_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )


def downgrade() -> None:
    op.drop_table("email_ignores")
    op.drop_table("google_change_log")
    op.drop_table("roster_changes")
