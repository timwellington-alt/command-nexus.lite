"""Add demographic fields to roster_snapshots for SWIS PBIS Person Import.

MetaSolutions already ships these columns in Clever-Students.csv — we just
haven't been storing them. Adding to roster_snapshots so the SWIS Student
exporter has the required + optional fields SWIS asks for:

  gender          → SWIS genderId (M/F/X)
  race            → SWIS race (single letter EMIS code)
  hispanic_latino → SWIS isHispanic (Y/N)
  ell_status      → SWIS isLanguageLearner (Y/N)
  iep_status      → SWIS hasIEP (Y/N)

All nullable text. Populated on next clever_import_job run.

Revision ID: a184_roster_swis_fields
Revises: a183_roster_deprov_exemptions
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a184_roster_swis_fields"
down_revision = "a183_roster_deprov_exemptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("roster_snapshots", sa.Column("gender", sa.String(10), nullable=True))
    op.add_column("roster_snapshots", sa.Column("race", sa.String(10), nullable=True))
    op.add_column("roster_snapshots", sa.Column("hispanic_latino", sa.String(5), nullable=True))
    op.add_column("roster_snapshots", sa.Column("ell_status", sa.String(5), nullable=True))
    op.add_column("roster_snapshots", sa.Column("iep_status", sa.String(5), nullable=True))


def downgrade() -> None:
    op.drop_column("roster_snapshots", "iep_status")
    op.drop_column("roster_snapshots", "ell_status")
    op.drop_column("roster_snapshots", "hispanic_latino")
    op.drop_column("roster_snapshots", "race")
    op.drop_column("roster_snapshots", "gender")
