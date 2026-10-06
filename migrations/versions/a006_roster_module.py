"""Roster module tables — imports, snapshots, student_teachers, guidance_queue.

Revision ID: a006_roster
Revises: a005_access
Create Date: 2026-03-28
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a006_roster"
down_revision: Union[str, None] = "a005_access"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── roster_imports ────────────────────────────────────────────────
    op.create_table(
        "roster_imports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("filename", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("total_records", sa.Integer(), server_default="0", nullable=False),
        sa.Column("added", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("removed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("started_by", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_roster_imports_status", "roster_imports", ["status"])
    op.create_index("ix_roster_imports_started_at", "roster_imports", ["started_at"])

    # ── roster_snapshots ──────────────────────────────────────────────
    op.create_table(
        "roster_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_id", sa.Integer(), sa.ForeignKey("roster_imports.id", ondelete="SET NULL"), nullable=True),
        # T1: Identity
        sa.Column("sis_id", sa.String(50), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("middle_name", sa.String(100), nullable=True),
        sa.Column("dob", sa.String(20), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        # T2: Enrollment
        sa.Column("school", sa.String(50), nullable=False),
        sa.Column("grade", sa.String(10), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("enrollment_date", sa.String(20), nullable=True),
        sa.Column("withdrawal_date", sa.String(20), nullable=True),
        # T4: Contacts (restricted)
        sa.Column("parent_guardian", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        # T5: Account state
        sa.Column("google_status", sa.String(30), nullable=True),
        sa.Column("email_compliant", sa.Boolean(), nullable=True),
        sa.Column("issue_tags", sa.Text(), nullable=True),
        # Metadata
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sis_id"),
    )
    op.create_index("ix_roster_snapshots_sis_id", "roster_snapshots", ["sis_id"])
    op.create_index("ix_roster_snapshots_school", "roster_snapshots", ["school"])
    op.create_index("ix_roster_snapshots_grade", "roster_snapshots", ["grade"])
    op.create_index("ix_roster_snapshots_status", "roster_snapshots", ["status"])
    op.create_index("ix_roster_snapshots_email", "roster_snapshots", ["email"])
    op.create_index("ix_roster_snapshots_import_id", "roster_snapshots", ["import_id"])
    op.create_index("ix_roster_snapshots_last_name", "roster_snapshots", ["last_name"])

    # ── student_teachers ──────────────────────────────────────────────
    op.create_table(
        "student_teachers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("roster_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("teacher_name", sa.String(255), nullable=False),
        sa.Column("teacher_email", sa.String(255), nullable=True),
        sa.Column("section_name", sa.String(100), nullable=True),
        sa.Column("homeroom", sa.String(50), nullable=True),
        sa.Column("period", sa.String(20), nullable=True),
        sa.Column("school", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_student_teachers_student_id", "student_teachers", ["student_id"])
    op.create_index("ix_student_teachers_teacher_name", "student_teachers", ["teacher_name"])
    op.create_index("ix_student_teachers_school", "student_teachers", ["school"])

    # ── guidance_queue ────────────────────────────────────────────────
    op.create_table(
        "guidance_queue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("roster_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("school", sa.String(50), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("priority", sa.String(20), server_default="normal", nullable=False),
        sa.Column("status", sa.String(20), server_default="open", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("assigned_to", sa.String(255), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_guidance_queue_student_id", "guidance_queue", ["student_id"])
    op.create_index("ix_guidance_queue_school", "guidance_queue", ["school"])
    op.create_index("ix_guidance_queue_status", "guidance_queue", ["status"])
    op.create_index("ix_guidance_queue_assigned_to", "guidance_queue", ["assigned_to"])


def downgrade() -> None:
    op.drop_table("guidance_queue")
    op.drop_table("student_teachers")
    op.drop_table("roster_snapshots")
    op.drop_table("roster_imports")
