"""Wave VMS cameras — tables + permissions.

Creates cameras, camera_events, and door_camera_mappings tables.
Adds security.cameras.view and security.cameras.manage permissions.

Revision ID: a043_wave_cameras
Revises: a042_security_module
Create Date: 2026-04-14
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a043_wave_cameras"
down_revision: Union[str, None] = "a042_security_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cameras",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wave_camera_id", sa.String(100), nullable=False, index=True),
        sa.Column("server_id", sa.Integer, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("model", sa.String(255)),
        sa.Column("vendor", sa.String(255)),
        sa.Column("mac", sa.String(50)),
        sa.Column("ip_address", sa.String(50)),
        sa.Column("building", sa.String(50), index=True),
        sa.Column("status", sa.String(50), server_default="Unknown"),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("wave_camera_id", "server_id", name="uq_camera_wave_server"),
    )

    op.create_table(
        "camera_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id"), nullable=False, index=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("details", sa.Text),
        sa.Column("acknowledged", sa.Boolean, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "door_camera_mappings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("door_id", sa.Integer, sa.ForeignKey("doors.id"), nullable=False),
        sa.Column("camera_id", sa.Integer, sa.ForeignKey("cameras.id"), nullable=False),
        sa.UniqueConstraint("door_id", name="uq_door_camera_door"),
    )

    # Permissions
    for action, desc in [
        ("security.cameras.view", "View camera inventory and live feeds"),
        ("security.cameras.manage", "Manage camera-door mappings and bookmarks"),
    ]:
        op.execute(f"""
            INSERT INTO permissions (action, description, is_student_sensitive)
            VALUES ('{action}', '{desc}', false)
            ON CONFLICT (action) DO NOTHING
        """)
        op.execute(f"""
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id FROM roles r, permissions p
            WHERE r.name = 'admin' AND p.action = '{action}'
            ON CONFLICT DO NOTHING
        """)


def downgrade() -> None:
    op.drop_table("door_camera_mappings")
    op.drop_table("camera_events")
    op.drop_table("cameras")
    for action in ["security.cameras.view", "security.cameras.manage"]:
        op.execute(f"""
            DELETE FROM role_permissions
            WHERE permission_id IN (SELECT id FROM permissions WHERE action = '{action}')
        """)
        op.execute(f"DELETE FROM permissions WHERE action = '{action}'")
