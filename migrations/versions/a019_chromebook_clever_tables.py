"""Add chromebook_cache and clever_processed tables.

Revision ID: a019_chromebook_clever
Revises: a018_seed_provisioning_profiles
Create Date: 2026-04-02
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a019_chromebook_clever"
down_revision: Union[str, None] = "a018_seed_provisioning_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Chromebook device cache
    op.create_table(
        "chromebook_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("serial", sa.String(100), nullable=False, index=True),
        sa.Column("model", sa.String(255)),
        sa.Column("status", sa.String(50), index=True),
        sa.Column("org_unit", sa.String(500), index=True),
        sa.Column("annotated_user", sa.String(255)),
        sa.Column("annotated_location", sa.String(255)),
        sa.Column("annotated_asset_id", sa.String(100), index=True),
        sa.Column("os_version", sa.String(100)),
        sa.Column("platform_version", sa.String(100)),
        sa.Column("firmware_version", sa.String(100)),
        sa.Column("mac_address", sa.String(50)),
        sa.Column("ethernet_mac", sa.String(50)),
        sa.Column("boot_mode", sa.String(50)),
        sa.Column("last_sync", sa.String(50)),
        sa.Column("last_enrollment", sa.String(50)),
        sa.Column("auto_update_expiration", sa.String(50)),
        sa.Column("notes", sa.Text),
        sa.Column("last_user", sa.String(255)),
        sa.Column("last_network_address", sa.String(50)),
        sa.Column("last_network_wan", sa.String(50)),
        sa.Column("system_ram_total", sa.String(50)),
        sa.Column("manufacture_date", sa.String(50)),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Clever processed message tracking
    op.create_table(
        "clever_processed",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("msg_id", sa.String(255), unique=True, nullable=False),
        sa.Column("subject", sa.String(500)),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Chromebook permissions + role (idempotent via ON CONFLICT)
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES ('chromebook.view', 'View Chromebook inventory', false),
               ('chromebook.manage', 'Manage Chromebook assignments and actions', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO roles (name, description)
        VALUES ('chromebook_admin', 'Chromebook management — building-scoped device maintenance')
        ON CONFLICT (name) DO NOTHING
    """)
    # Wire permissions to chromebook_admin and admin roles
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name IN ('chromebook_admin', 'admin')
        AND p.action IN ('chromebook.view', 'chromebook.manage')
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name = 'chromebook_admin'
        AND p.action IN ('dashboard.view', 'audit.view')
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table("clever_processed")
    op.drop_table("chromebook_cache")
    op.execute("DELETE FROM permissions WHERE action IN ('chromebook.view', 'chromebook.manage')")
    op.execute("DELETE FROM roles WHERE name = 'chromebook_admin'")
