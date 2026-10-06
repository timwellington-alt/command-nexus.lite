"""Student Drive scanner — schema + permissions.

Crawls student Google Drive accounts for documents containing URLs to
proxy / VPN / unblocked-game site lists. Hits feed a review queue
(nuke the doc / whitelist / skip per row) plus a domain rollup that
exports as a CSV ready for paste into GoGuardian's bulk-upload tool.

Tables:
    student_scan_runs       — per scan execution
    student_scan_hits       — one row per (doc × student-with-access)
    student_scan_domains    — unique-domain rollup with GG add status
    student_scan_whitelist  — per-doc + per-student exemptions

Permissions live under operations.student_scan.*

Revision ID: a094_student_scan
Revises: a093_bus_dvr_protocol
Create Date: 2026-05-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "a094_student_scan"
down_revision: Union[str, None] = "a093_bus_dvr_protocol"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "student_scan_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # pending → running → complete | partial | failed
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("scope_description", sa.String(200), nullable=True),
        sa.Column("students_scanned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("docs_scanned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("hits_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_by", sa.String(255), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','running','complete','partial','failed','cancelled')",
            name="ck_student_scan_runs_status",
        ),
    )

    op.create_table(
        "student_scan_hits",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "scan_run_id", sa.Integer,
            sa.ForeignKey("student_scan_runs.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("student_email", sa.String(255), nullable=False, index=True),
        sa.Column("doc_id", sa.String(120), nullable=False, index=True),
        sa.Column("doc_title", sa.Text, nullable=True),
        sa.Column("doc_kind", sa.String(20), nullable=True),  # 'document' | 'spreadsheet'
        sa.Column("owner_email", sa.String(255), nullable=True, index=True),
        sa.Column("owner_role", sa.String(20), nullable=True),  # 'student' | 'staff' | 'external'
        # JSONB so we can render the full URL list per row in the
        # review UI without joining a per-URL child table.
        sa.Column("matched_urls", JSONB, nullable=False, server_default="[]"),
        sa.Column("keyword_triggers", JSONB, nullable=False, server_default="[]"),
        # none → trashed | deleted | access_revoked | whitelisted
        sa.Column("action_taken", sa.String(30), nullable=False, server_default="none"),
        sa.Column("acted_by", sa.String(255), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("scan_run_id", "student_email", "doc_id",
                            name="uq_student_scan_hits_run_student_doc"),
        sa.CheckConstraint(
            "action_taken IN ('none','trashed','deleted','access_revoked','whitelisted','skipped')",
            name="ck_student_scan_hits_action",
        ),
    )
    # Cross-run lookup: "has this doc been acted on before?"
    op.create_index(
        "ix_student_scan_hits_doc_action",
        "student_scan_hits", ["doc_id", "action_taken"],
    )

    op.create_table(
        "student_scan_domains",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("occurrences", sa.Integer, nullable=False, server_default="1"),
        sa.Column("distinct_students", sa.Integer, nullable=False, server_default="1"),
        # pending → added | ignored | whitelisted
        sa.Column("gg_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("acted_by", sa.String(255), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.UniqueConstraint("domain", name="uq_student_scan_domains_domain"),
        sa.CheckConstraint(
            "gg_status IN ('pending','added','ignored','whitelisted')",
            name="ck_student_scan_domains_status",
        ),
    )

    op.create_table(
        "student_scan_whitelist",
        sa.Column("id", sa.Integer, primary_key=True),
        # Either doc_id (specific doc OK'd) OR student_email (specific
        # student's docs always OK — e.g. their cybersecurity class
        # has legitimate proxy refs). Exactly one of the two is set.
        sa.Column("doc_id", sa.String(120), nullable=True),
        sa.Column("student_email", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("added_by", sa.String(255), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "(doc_id IS NULL) <> (student_email IS NULL)",
            name="ck_student_scan_whitelist_one_of_two",
        ),
        sa.UniqueConstraint("doc_id", name="uq_student_scan_whitelist_doc"),
        sa.UniqueConstraint("student_email", name="uq_student_scan_whitelist_student"),
    )

    # ── Permissions ────────────────────────────────────────────────
    for action, desc in [
        ("operations.student_scan.view",
         "View student Drive scan results, hits, offenders, and domain rollup"),
        ("operations.student_scan.act",
         "Trash flagged docs, revoke shares, mark GG domains as added"),
        ("operations.student_scan.admin",
         "Manage whitelist + scan settings + initiate scans"),
    ]:
        op.execute(f"""
            INSERT INTO permissions (action, description, is_student_sensitive)
            VALUES ('{action}', '{desc}', true)
            ON CONFLICT (action) DO NOTHING
        """)
        op.execute(f"""
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id FROM roles r, permissions p
            WHERE r.name = 'admin' AND p.action = '{action}'
            ON CONFLICT DO NOTHING
        """)


def downgrade() -> None:
    op.drop_table("student_scan_whitelist")
    op.drop_table("student_scan_domains")
    op.drop_index("ix_student_scan_hits_doc_action", table_name="student_scan_hits")
    op.drop_table("student_scan_hits")
    op.drop_table("student_scan_runs")
    for action in (
        "operations.student_scan.view",
        "operations.student_scan.act",
        "operations.student_scan.admin",
    ):
        op.execute(f"""
            DELETE FROM role_permissions WHERE permission_id IN
                (SELECT id FROM permissions WHERE action = '{action}')
        """)
        op.execute(f"DELETE FROM permissions WHERE action = '{action}'")
