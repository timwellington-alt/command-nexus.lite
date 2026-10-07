"""Staff photo library — DB-backed metadata.

Previously staff photos lived purely on disk with filename-encoded
state (which was active, when uploaded, who uploaded) and Paxton as
a de-facto second source via the ``paxton_{paxton_id}.jpg`` fallback
in the gallery.

a204 moves photo metadata into Postgres so Nexus is the system of
record. Files still live on disk under ``data/photos/`` (binary in
DB is a bad idea); the row carries the on-disk filename plus the
state Nexus actually needs (is_active, uploaded_by, uploaded_at,
source).

Enables a future Paxton push-sync worker to be a one-file bolt-on
that reads ``WHERE source='upload' AND NOT paxton_pushed`` — but
no coupling from upload → Paxton in the core flow.

Revision ID: a204_staff_photos
Revises: a203_local_users
Create Date: 2026-10-07
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a204_staff_photos"
down_revision: Union[str, None] = "a203_local_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_photos",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # staff_username is a plain varchar — no FK to staff_reconciliation
        # so this table survives any future replacement of the recon
        # layer. The lookup side joins by lower(username).
        sa.Column("staff_username", sa.String(100), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("uploaded_by", sa.String(255), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        # source: 'upload' | 'self' | 'import'. Lets a future Paxton
        # push-sync worker query 'WHERE source IN (...) AND NOT synced'.
        sa.Column("source", sa.String(32), nullable=False, server_default="upload"),
        sa.Column("bytes", sa.Integer()),
    )
    op.create_index(
        "ix_staff_photos_username",
        "staff_photos",
        [sa.text("lower(staff_username)")],
    )
    # At most one active photo per staff member — PG partial unique index.
    op.create_index(
        "ux_staff_photos_active_per_staff",
        "staff_photos",
        [sa.text("lower(staff_username)")],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("ux_staff_photos_active_per_staff", table_name="staff_photos")
    op.drop_index("ix_staff_photos_username", table_name="staff_photos")
    op.drop_table("staff_photos")
