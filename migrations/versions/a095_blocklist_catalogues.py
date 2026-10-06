"""External blocklist catalogues — TBLP-style host-list cache.

Stores fetched copies of open-source domain blocklists (The Block List
Project's `proxy.txt`, `games.txt`, etc.) so the student-Drive scanner
can match against them without hitting the network on every run.

Tables:
    blocklist_catalogues  — one row per source list (key, source_url,
                            fetched_at, entry_count, etag, last_error)
    blocklist_domains     — flattened (catalogue_id, domain) pairs

Refreshed daily by the `refresh_blocklist_catalogues` ARQ job. If the
fetch fails, the previous rows are kept (the matcher always reads the
last-good copy, never an empty list mid-refresh).

Revision ID: a095_blocklist_catalogues
Revises: a094_student_scan
Create Date: 2026-05-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a095_blocklist_catalogues"
down_revision: Union[str, None] = "a094_student_scan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blocklist_catalogues",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("key", sa.String(40), nullable=False, unique=True),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("etag", sa.String(120), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
    )

    op.create_table(
        "blocklist_domains",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "catalogue_id", sa.Integer,
            sa.ForeignKey("blocklist_catalogues.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.UniqueConstraint("catalogue_id", "domain",
                            name="uq_blocklist_domains_catalogue_domain"),
    )
    op.create_index(
        "ix_blocklist_domains_domain",
        "blocklist_domains", ["domain"],
    )


def downgrade() -> None:
    op.drop_index("ix_blocklist_domains_domain", table_name="blocklist_domains")
    op.drop_table("blocklist_domains")
    op.drop_table("blocklist_catalogues")
