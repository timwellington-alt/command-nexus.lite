"""Persistent camera-to-Chromecast mappings.

One row per (Chromecast, camera) assignment we want to keep pushed
24/7. A background worker polls each mapping and re-casts when the
target device drops off or times out.

Revision ID: a199_cast_camera_streams
Revises: a198_device_kinds
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a199_cast_camera_streams"
down_revision = "a198_device_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cast_persistent_streams",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("chromecast_id", sa.Integer, sa.ForeignKey("cast_devices.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("camera_id", sa.String(100), nullable=False),
        sa.Column("camera_label", sa.String(200), nullable=False, server_default=""),
        sa.Column("wave_server_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index(
        "ux_cast_persistent_streams",
        "cast_persistent_streams",
        ["chromecast_id"],
        unique=True,
    )
    op.execute("""
        INSERT INTO permissions (action, description)
        VALUES ('network.cast_camera.manage',
                'Assign a Wisenet camera stream to a Chromecast for persistent 24/7 display')
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT 1, id FROM permissions WHERE action = 'network.cast_camera.manage'
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM permissions WHERE action = 'network.cast_camera.manage'")
    op.drop_index("ux_cast_persistent_streams", table_name="cast_persistent_streams")
    op.drop_table("cast_persistent_streams")
