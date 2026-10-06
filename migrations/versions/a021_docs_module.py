"""Add vendor_contracts, emergency_contacts tables and docs permissions.

Revision ID: a021_docs_module
Revises: a020_phone_cache
Create Date: 2026-04-03
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a021_docs_module"
down_revision: Union[str, None] = "a020_phone_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vendor_contracts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vendor_name", sa.String(255), nullable=False, index=True),
        sa.Column("service", sa.String(255)),
        sa.Column("contact_name", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column("email", sa.String(255)),
        sa.Column("account_number", sa.String(100)),
        sa.Column("contract_start", sa.Date),
        sa.Column("contract_end", sa.Date, index=True),
        sa.Column("renewal_terms", sa.Text),
        sa.Column("annual_cost", sa.Numeric(12, 2)),
        sa.Column("category", sa.String(100), index=True),
        sa.Column("notes", sa.Text),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "emergency_contacts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("organization", sa.String(255)),
        sa.Column("role", sa.String(255)),
        sa.Column("phone", sa.String(50)),
        sa.Column("email", sa.String(255)),
        sa.Column("account_number", sa.String(100)),
        sa.Column("circuit_id", sa.String(100)),
        sa.Column("category", sa.String(50), nullable=False, index=True),
        sa.Column("priority", sa.Integer, default=0, index=True),
        sa.Column("notes", sa.Text),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Permissions
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES ('docs.view', 'View documentation and operations', false),
               ('docs.manage', 'Manage docs, vendors, and contacts', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name = 'admin'
        AND p.action IN ('docs.view', 'docs.manage')
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table("emergency_contacts")
    op.drop_table("vendor_contracts")
    op.execute("DELETE FROM permissions WHERE action IN ('docs.view', 'docs.manage')")
