"""Local-auth user accounts (feature-flagged via LOCAL_AUTH_ENABLED).

Secondary auth mode alongside Google SSO. Users here are stored
directly in the DB (argon2id-hashed password). The /auth/local/login
route only lights up when LOCAL_AUTH_ENABLED=true in the env.

Revision ID: a203_local_users
Revises: a202_cast_stream_quarantine
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a203_local_users"
down_revision = "a202_cast_stream_quarantine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255)),
        sa.Column("pw_hash", sa.String(255), nullable=False),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("created_by", sa.String(255)),
        sa.Column("last_login", sa.DateTime(timezone=True)),
        sa.Column("must_change_password", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index("ix_local_users_email_ci", "local_users",
                    [sa.text("lower(email)")], unique=True)


def downgrade() -> None:
    op.drop_index("ix_local_users_email_ci", table_name="local_users")
    op.drop_table("local_users")
