"""ISO-formatted AUE date on chromebook_cache so we can sort + filter.

Google's Admin SDK returns ``autoUpdateExpiration`` as epoch milliseconds
(a stringified integer like ``"1811833200000"``). Storing that verbatim
means every UI ordering + comparison has to convert on the fly and
string-comparing ``"1527836400000"`` against ``"2026-07-29"`` puts every
row in the wrong bucket.

Adding a companion ``aue_date`` column populated from the epoch on each
sync. Kept nullable so devices whose Google record omits the field don't
break the index.

Revision ID: a151_chromebook_aue_date
Revises: a150_hr_cert_number
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a151_chromebook_aue_date"
down_revision = "a150_hr_cert_number"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chromebook_cache",
        sa.Column("aue_date", sa.String(10), nullable=True),
    )
    op.create_index(
        "ix_chromebook_cache_aue_date",
        "chromebook_cache",
        ["aue_date"],
    )
    # Backfill from the epoch-ms strings we already have.
    op.execute(
        """
        UPDATE chromebook_cache
           SET aue_date = TO_CHAR(
                   TO_TIMESTAMP(CAST(auto_update_expiration AS BIGINT) / 1000),
                   'YYYY-MM-DD'
               )
         WHERE auto_update_expiration IS NOT NULL
           AND auto_update_expiration <> ''
           AND auto_update_expiration ~ '^[0-9]+$'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_chromebook_cache_aue_date", "chromebook_cache")
    op.drop_column("chromebook_cache", "aue_date")
