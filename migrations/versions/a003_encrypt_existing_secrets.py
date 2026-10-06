"""
a003 — Mark existing integration secret rows as requiring re-entry.

Rows with is_secret_ref=True were stored before application-level encryption
was introduced. They contain plaintext values that cannot be safely encrypted
retroactively without the encryption key at migration time.

OPERATOR ACTION REQUIRED after deploying this migration:
1. Ensure settings_encryption_key is configured in Docker secrets
2. Log in to Settings and re-enter all secret fields (API tokens, passwords)
3. The Settings UI will encrypt them on save using Fernet

This is intentional — clearing the values forces re-entry through the
encrypted write path. There is no automated re-encryption.

Revision ID: a003_encrypt
Revises: a002_seed
Create Date: 2026-03-27
"""

from typing import Sequence, Union
from alembic import op

revision: str = "a003_encrypt"
down_revision: Union[str, None] = "a002_seed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE integration_configs SET value = NULL WHERE is_secret_ref = TRUE"
    )


def downgrade() -> None:
    pass  # Cannot restore cleared values — intentional
