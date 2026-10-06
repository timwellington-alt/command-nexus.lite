"""Staff module tables — requests, workflow runs, steps, links.

Revision ID: a004_staff
Revises: a003_encrypt
Create Date: 2026-03-27
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a004_staff"
down_revision: Union[str, None] = "a003_encrypt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Staff requests
    op.create_table(
        "staff_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_type", sa.String(20), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("building", sa.String(20), nullable=False),
        sa.Column("role_type", sa.String(30), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("start_date", sa.String(20), nullable=True),
        sa.Column("state_id", sa.String(50), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("room_number", sa.String(20), nullable=True),
        sa.Column("needs_sis", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("photo_path", sa.String(500), nullable=True),
        sa.Column("existing_email", sa.String(255), nullable=True),
        sa.Column("existing_ad_username", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("submitted_by", sa.String(255), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("provisioned_by", sa.String(255), nullable=True),
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sis_confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sis_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # Workflow runs
    op.create_table(
        "staff_workflow_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("staff_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), server_default="running", nullable=False),
        sa.Column("started_by", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_staff_workflow_runs_request_id", "staff_workflow_runs", ["request_id"])

    # Workflow steps
    op.create_table(
        "staff_workflow_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("staff_workflow_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_name", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("before_state", sa.Text(), nullable=True),
        sa.Column("after_state", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result_data", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_staff_workflow_steps_run_id", "staff_workflow_steps", ["run_id"])

    # Staff links
    op.create_table(
        "staff_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ad_username", sa.String(255), nullable=True),
        sa.Column("paxton_id", sa.Integer(), nullable=True),
        sa.Column("google_email", sa.String(255), nullable=True),
        sa.Column("match_type", sa.String(20), server_default="auto", nullable=False),
        sa.Column("confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confirmed_by", sa.String(255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_staff_links_ad_username", "staff_links", ["ad_username"])
    op.create_index("ix_staff_links_paxton_id", "staff_links", ["paxton_id"])
    op.create_index("ix_staff_links_google_email", "staff_links", ["google_email"])


def downgrade() -> None:
    op.drop_table("staff_links")
    op.drop_table("staff_workflow_steps")
    op.drop_table("staff_workflow_runs")
    op.drop_table("staff_requests")
