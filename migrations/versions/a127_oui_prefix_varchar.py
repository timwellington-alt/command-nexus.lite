"""Widen oui_registry.prefix from CHAR(6) to VARCHAR(9).

Companion to a126 — that migration added prefix_bits but didn't change
the column type. MA-M (7) and MA-S (9) prefix lengths overflow CHAR(6).

Revision ID: a127_oui_prefix_varchar
Revises: a126_oui_registry_bits
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a127_oui_prefix_varchar"
down_revision = "a126_oui_registry_bits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop and recreate any PK / unique constraints on prefix only if
    # needed; ALTER TYPE on PG works in place with no rewrite.
    op.alter_column(
        "oui_registry",
        "prefix",
        type_=sa.String(9),
        existing_type=sa.CHAR(6),
        nullable=False,
    )


def downgrade() -> None:
    op.execute("DELETE FROM oui_registry WHERE LENGTH(prefix) > 6")
    op.alter_column(
        "oui_registry",
        "prefix",
        type_=sa.CHAR(6),
        existing_type=sa.String(9),
        nullable=False,
    )
