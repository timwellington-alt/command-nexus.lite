"""Manual network devices — switches not discovered via LibreNMS.

For devices that aren't in LibreNMS (the core, vendor-managed kit, EOL
gear) but still need config backups. Each row carries optional per-device
SSH credentials; when blank, the global switch_ssh.* fallback applies.
Passwords are Fernet-encrypted at the column level using the same key as
integration_configs.is_secret_ref.

Revision ID: a063_network_manual_devices
Revises: a062_inv_doc_kind
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a063_network_manual_devices"
down_revision: Union[str, None] = "a062_inv_doc_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "network_manual_devices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ip", sa.String(45), nullable=False, unique=True),
        sa.Column("hostname", sa.String(255)),
        sa.Column("hardware", sa.String(255)),
        sa.Column("device_type", sa.String(50), nullable=False, server_default="autodetect"),
        sa.Column("username", sa.String(255)),
        sa.Column("password_encrypted", sa.Text()),
        sa.Column("enable_password_encrypted", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("last_backup_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_network_manual_devices_ip", "network_manual_devices", ["ip"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_network_manual_devices_ip", table_name="network_manual_devices")
    op.drop_table("network_manual_devices")
