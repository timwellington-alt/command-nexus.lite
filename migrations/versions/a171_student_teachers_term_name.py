"""Capture SIS term_name on student_teachers.

Clever's sections CSV carries a ``Term_name`` field with values
``year``, ``SEM1``, ``SEM2``, ``TRI1``/``TRI2``/``TRI3``, etc. Storing
it lets the student-schedule view filter to the active semester
authoritatively instead of parsing A/B suffixes off course names.

Nullable — populated as sections re-import from Clever. Existing rows
stay NULL until then, and the filter falls back to its
course-name-suffix heuristic when term_name is missing.

Revision ID: a171_student_teachers_term_name
Revises: a170_damage_item_chromebook_link
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a171_student_teachers_term_name"
down_revision = "a170_damage_item_chromebook_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "student_teachers",
        sa.Column("term_name", sa.String(20), nullable=True),
    )
    op.create_index("ix_student_teachers_term_name",
                    "student_teachers", ["term_name"])


def downgrade() -> None:
    op.drop_index("ix_student_teachers_term_name",
                  table_name="student_teachers")
    op.drop_column("student_teachers", "term_name")
