"""REI bus footage offload — DVR registry + incident-driven request queue.

Workflow:
  1. SRO/admin submits a request: "Bus 40, 14:30-14:36 today, incident #X"
  2. A scheduled worker checks each pending request's target bus DVR
     for online status. When the bus is reachable, it pulls the matching
     RECORDIDs via REQUESTDOWNLOADVIDEO, assembles an HD5 file, and
     marks the request fulfilled with the NAS path.
  3. The SRO opens the HD5 file in REI VMS to view/export segments.

No bulk pre-pulls; nothing is downloaded until flagged for an incident.

Revision ID: a091_bus_footage
Revises: a090_planning_cameras
Create Date: 2026-05-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a091_bus_footage"
down_revision: Union[str, None] = "a090_planning_cameras"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bus_dvrs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("carnum", sa.String(20), nullable=False),
        sa.Column("name", sa.String(80), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=False),
        # Per-DVR password override. NULL = use settings rei.default_password.
        sa.Column("password_override", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        # Last identity captured from a successful LOGIN — rendered in UI
        # so admins can see firmware/serial without manually polling.
        sa.Column("last_dsno", sa.String(40), nullable=True),
        sa.Column("last_channel_count", sa.Integer, nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_online_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("carnum", name="uq_bus_dvrs_carnum"),
        sa.UniqueConstraint("ip_address", name="uq_bus_dvrs_ip"),
    )

    op.create_table(
        "bus_footage_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "bus_dvr_id", sa.Integer,
            sa.ForeignKey("bus_dvrs.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        # Operator-supplied incident window. The pipeline pads this on
        # fetch (default 2 min before, 5 min after — see service.py).
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
        # Padded window actually pulled. Computed at fulfillment time.
        sa.Column("padded_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("padded_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requester_email", sa.String(255), nullable=False),
        sa.Column("incident_ref", sa.String(80), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        # pending → in_progress → fulfilled | failed
        sa.Column("status", sa.String(20), nullable=False, server_default="pending", index=True),
        sa.Column("hd5_path", sa.String(1024), nullable=True),
        sa.Column("hd5_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        # Set when a worker grabs the row to start fulfillment. Acts as a
        # lease so a second worker doesn't double-fetch on retry.
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','fulfilled','failed','cancelled')",
            name="ck_bus_footage_requests_status",
        ),
        sa.CheckConstraint(
            "requested_end > requested_start",
            name="ck_bus_footage_requests_time_order",
        ),
    )
    op.create_index(
        "ix_bus_footage_requests_pending",
        "bus_footage_requests",
        ["status", "created_at"],
    )

    # ── Permissions ────────────────────────────────────────────────
    for action, desc in [
        ("security.footage.view",
         "View bus footage requests + download fulfilled HD5 files"),
        ("security.footage.request",
         "Submit incident-driven bus footage requests"),
        ("security.footage.admin",
         "Manage bus DVR registry + delete/cancel any request"),
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
    op.drop_index("ix_bus_footage_requests_pending", table_name="bus_footage_requests")
    op.drop_table("bus_footage_requests")
    op.drop_table("bus_dvrs")
    for action in (
        "security.footage.view",
        "security.footage.request",
        "security.footage.admin",
    ):
        op.execute(f"""
            DELETE FROM role_permissions WHERE permission_id IN
                (SELECT id FROM permissions WHERE action = '{action}')
        """)
        op.execute(f"DELETE FROM permissions WHERE action = '{action}'")
