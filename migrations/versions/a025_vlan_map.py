"""Add VLAN map tables for traversal map and ACL matrix.

Revision ID: a025_vlan_map
Revises: a024_config_backups
Create Date: 2026-04-06
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a025_vlan_map"
down_revision: Union[str, None] = "a024_config_backups"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vlan_map_entries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vlan_id", sa.Integer, unique=True, nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("subnet", sa.String(50)),
        sa.Column("gateway_ip", sa.String(50)),
        sa.Column("acl_in", sa.String(100)),
        sa.Column("acl_out", sa.String(100)),
        sa.Column("dhcp_helpers", sa.Text),
        sa.Column("tagged_ports", sa.Text),
        sa.Column("untagged_ports", sa.Text),
        sa.Column("group_name", sa.String(50)),
        sa.Column("notes", sa.Text),
        sa.Column("internet_access", sa.String(20)),
        sa.Column("parsed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "vlan_acl_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("acl_name", sa.String(100), nullable=False, index=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("protocol", sa.String(20)),
        sa.Column("source", sa.String(100)),
        sa.Column("source_mask", sa.String(50)),
        sa.Column("destination", sa.String(100)),
        sa.Column("dest_mask", sa.String(50)),
        sa.Column("dest_port", sa.String(50)),
        sa.Column("raw_line", sa.Text),
        sa.Column("parsed_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "vlan_traversal_overrides",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("from_vlan", sa.Integer, nullable=False),
        sa.Column("to_vlan", sa.Integer, nullable=False),
        sa.Column("access_level", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("updated_by", sa.String(255)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("vlan_traversal_overrides")
    op.drop_table("vlan_acl_rules")
    op.drop_table("vlan_map_entries")
