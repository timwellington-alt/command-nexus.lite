"""Add staff_reconciliation table for pre-computed cross-system matching.

Revision ID: a027_staff_reconciliation
Revises: a026_wireless_clients
Create Date: 2026-04-07
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a027_staff_reconciliation"
down_revision: Union[str, None] = "a026_wireless_clients"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_reconciliation",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),

        # Identity (from Google)
        sa.Column("email", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("username", sa.String(255), nullable=False, index=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("full_name", sa.String(255)),
        sa.Column("display_name", sa.String(255)),
        sa.Column("title", sa.String(255)),
        sa.Column("department", sa.String(100)),
        sa.Column("building", sa.String(50), index=True),
        sa.Column("org_unit", sa.String(500)),
        sa.Column("phone", sa.String(50)),
        sa.Column("google_status", sa.String(20), server_default="active"),
        sa.Column("is_admin", sa.Boolean, server_default="false"),
        sa.Column("last_login", sa.String(50)),
        sa.Column("google_id", sa.String(100)),

        # Matched IDs
        sa.Column("paxton_id", sa.Integer),
        sa.Column("ad_username", sa.String(255)),
        sa.Column("extension", sa.String(20)),
        sa.Column("room", sa.String(50)),
        sa.Column("room_assignment", sa.String(255)),
        sa.Column("room_floor", sa.String(50)),
        sa.Column("room_building", sa.String(50)),
        sa.Column("is_esc", sa.Boolean, server_default="false"),
        sa.Column("hr_email", sa.String(255)),

        # Status flags
        sa.Column("google_ok", sa.Boolean, server_default="false"),
        sa.Column("ad_ok", sa.Boolean, nullable=True),
        sa.Column("sis_ok", sa.Boolean, nullable=True),
        sa.Column("hr_active", sa.Boolean, nullable=True),
        sa.Column("paxton_ok", sa.Boolean, server_default="false"),
        sa.Column("has_paxton_photo", sa.Boolean, server_default="false"),

        # Role
        sa.Column("role_type", sa.String(50)),
        sa.Column("hr_position", sa.String(255)),
        sa.Column("hr_school", sa.String(100)),
        sa.Column("hr_classification", sa.String(100)),

        # Match confidence
        sa.Column("paxton_match_method", sa.String(30)),
        sa.Column("ad_match_method", sa.String(30)),
        sa.Column("phone_match_method", sa.String(30)),
        sa.Column("room_match_method", sa.String(30)),

        # Names from each system
        sa.Column("paxton_name", sa.String(255)),
        sa.Column("ad_display_name", sa.String(255)),
        sa.Column("phone_caller_id", sa.String(255)),

        # Computed issues (JSON text)
        sa.Column("name_sync_issues", sa.Text),
        sa.Column("room_ext_mismatch", sa.Text),

        # Door access
        sa.Column("last_door_time", sa.String(50)),
        sa.Column("last_door_name", sa.String(255)),
        sa.Column("last_door_building", sa.String(50)),

        # Phone (static)
        sa.Column("phone_registered", sa.Boolean, nullable=True),
        sa.Column("phone_ip", sa.String(50)),
        sa.Column("phone_model", sa.String(100)),

        # Meta
        sa.Column("ignored", sa.Boolean, server_default="false"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("staff_reconciliation")
