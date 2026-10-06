"""Tech routing system — location-aware alert dispatch.

Creates tech_team, tech_push_subscriptions, and alert_buildings tables.
Pre-seeds buildings with known coordinates and permissions.

Revision ID: a044_tech_routing
Revises: a043_wave_cameras
Create Date: 2026-04-16
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a044_tech_routing"
down_revision: Union[str, None] = "a043_wave_cameras"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tech_team",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(50), nullable=False, unique=True),   # "tim", "mark", "chance"
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("phone_ext", sa.String(20)),
        sa.Column("phone_mobile", sa.String(20)),                         # E.164
        sa.Column("mac_address", sa.String(20)),                          # WiFi MAC of phone
        sa.Column("home_lat", sa.Float),
        sa.Column("home_lon", sa.Float),
        sa.Column("priority_order", sa.Integer, nullable=False, default=100),
        sa.Column("voice_recipient_id", sa.Integer, sa.ForeignKey("voice_recipients.id"), nullable=True),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "tech_push_subscriptions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tech_id", sa.Integer, sa.ForeignKey("tech_team.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("p256dh", sa.Text, nullable=False),
        sa.Column("auth", sa.Text, nullable=False),
        sa.Column("user_agent", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("endpoint", name="uq_push_endpoint"),
    )
    op.create_index("ix_tech_push_subs_tech_id", "tech_push_subscriptions", ["tech_id"])

    op.create_table(
        "alert_buildings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lon", sa.Float, nullable=False),
        sa.Column("tier", sa.Integer, nullable=False, server_default="1"),  # 1=critical, 3=low
        sa.Column("p1_device_pattern", sa.String(100)),                     # LibreNMS sysname prefix
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Buildings are configured through the Settings UI — no hardcoded data here.

    # Permissions
    for action, desc in [
        ("alerts.tech.view",   "View tech team roster and current locations"),
        ("alerts.tech.manage", "Manage tech team members and alert buildings"),
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
    op.drop_table("tech_push_subscriptions")
    op.drop_table("tech_team")
    op.drop_table("alert_buildings")
    for action in ["alerts.tech.view", "alerts.tech.manage"]:
        op.execute(f"""
            DELETE FROM role_permissions
            WHERE permission_id IN (SELECT id FROM permissions WHERE action = '{action}')
        """)
        op.execute(f"DELETE FROM permissions WHERE action = '{action}'")
