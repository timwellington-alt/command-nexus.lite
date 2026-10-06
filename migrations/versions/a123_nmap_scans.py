"""Nmap scan history + result cache.

Operator runs scans via the Network Tools UI. The scan runs as an arq
background job; the API record holds the requested options, the rendered
nmap command, status, raw XML output, and a parsed summary for the UI.

Revision ID: a123_nmap_scans
Revises: a122_district_calendar_dates
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a123_nmap_scans"
down_revision = "a122_district_calendar_dates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nmap_scans",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("target", sa.Text, nullable=False),
        sa.Column("options", sa.JSON, nullable=False),
        sa.Column("command", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.String(255), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_xml", sa.Text, nullable=True),
        sa.Column("raw_stderr", sa.Text, nullable=True),
        sa.Column("parsed_summary", sa.JSON, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_nmap_scans_queued_at", "nmap_scans", ["queued_at"])
    op.create_index("ix_nmap_scans_status", "nmap_scans", ["status"])


def downgrade() -> None:
    op.drop_index("ix_nmap_scans_status", table_name="nmap_scans")
    op.drop_index("ix_nmap_scans_queued_at", table_name="nmap_scans")
    op.drop_table("nmap_scans")
