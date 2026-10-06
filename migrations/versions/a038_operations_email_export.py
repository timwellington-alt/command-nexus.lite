"""Add operations module: email_exports table + operations permissions.

First concrete feature of the new Operations module — a Gmail-to-PDF
record-keeping exporter. Builds the ``email_exports`` tracking table
(history + audit trail for every export run) and seeds two new
permissions:

- ``operations.view``                — load the /operations page
- ``operations.email_export.execute`` — run a Gmail export

Both are granted to the admin role only. Future operations-area
features (log viewers, maintenance jobs, etc.) can reuse
``operations.view`` as the page-level gate and add their own
narrower execute permissions alongside.

The ``email_exports`` row carries everything the history view needs:
who ran it, whose mailbox, which label, why (free-text reason for
record keeping), PDF output path, counts, status, and any error. Rows
are created in ``pending`` state by the run endpoint and updated by
the ARQ worker job as it moves through ``running`` → ``complete`` /
``partial`` / ``failed``. No rows are deleted on downgrade — history
is load-bearing for compliance.

Revision ID: a038_ops_email_export
Revises: a037_guidance_notify
Create Date: 2026-04-10
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a038_ops_email_export"
down_revision: Union[str, None] = "a037_guidance_notify"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_exports",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # Who/what was exported
        sa.Column("target_user", sa.String(255), nullable=False, index=True),
        sa.Column("label", sa.String(255), nullable=False, index=True),
        sa.Column("reason", sa.Text, nullable=True),
        # Run options (captured at request time so history reflects
        # the exact parameters used, even if defaults shift later)
        sa.Column("skip_exported", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("mark_exported", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("email_report_to", sa.String(255), nullable=True),
        sa.Column("output_dir", sa.String(500), nullable=False),
        # Outputs
        sa.Column("pdf_path", sa.String(500), nullable=True),
        sa.Column("summary_path", sa.String(500), nullable=True),
        sa.Column("pdf_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attachment_count", sa.Integer, nullable=False, server_default="0"),
        # Lifecycle
        # pending → running → complete | partial | failed
        sa.Column("status", sa.String(20), nullable=False, server_default="pending", index=True),
        sa.Column("error", sa.Text, nullable=True),
        # Audit
        sa.Column("started_by", sa.String(255), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Composite index for the "recent exports" table sort
    op.create_index(
        "ix_email_exports_started_at_desc",
        "email_exports",
        [sa.text("started_at DESC")],
    )

    # ── Permissions ────────────────────────────────────────────────
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES
            ('operations.view', 'View the Operations page', false),
            ('operations.email_export.execute', 'Run Gmail record-keeping exports', false)
        ON CONFLICT (action) DO NOTHING
    """)

    # Grant both to the admin role
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name = 'admin'
          AND p.action IN ('operations.view', 'operations.email_export.execute')
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    # Drop permission grants first (FK dependency), then the
    # permissions themselves, then the table.
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions
            WHERE action IN ('operations.view', 'operations.email_export.execute')
        )
    """)
    op.execute("""
        DELETE FROM permissions
        WHERE action IN ('operations.view', 'operations.email_export.execute')
    """)
    op.drop_index("ix_email_exports_started_at_desc", table_name="email_exports")
    op.drop_table("email_exports")
