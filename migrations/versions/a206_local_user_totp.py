"""Add TOTP (RFC 6238) columns to local_users.

Nexus-lite enforces TOTP on every local-auth account:
  - totp_secret        — base32 secret stored unencrypted (same risk
                         tier as the pw_hash it lives next to; an
                         attacker with DB access can already auth as
                         any user by rewriting pw_hash, so TOTP
                         encryption adds no meaningful defense).
  - totp_verified_at   — set when the user completes enrollment by
                         entering a valid code. Login requires both
                         a valid password AND totp_verified_at IS NOT
                         NULL + a current 6-digit code.

Admin can reset TOTP for a user via Local Accounts (nulls both
columns); user is then forced through enrollment on next login.

Revision ID: a206_local_user_totp
Revises: a205_lite_settings_cleanup
Create Date: 2026-10-08
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a206_local_user_totp"
down_revision: Union[str, None] = "a205_lite_settings_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("local_users", sa.Column("totp_secret", sa.String(64)))
    op.add_column("local_users", sa.Column("totp_verified_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("local_users", "totp_verified_at")
    op.drop_column("local_users", "totp_secret")
