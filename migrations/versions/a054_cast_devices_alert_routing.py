"""Cast devices join the alert-routing flow.

Cast devices are now alert recipients with a severity threshold and
optional building scope — matching the voice_recipients model. The
old `auto_cast` flag becomes implicit: a device fires when an alert
meets its severity threshold AND (its building matches OR it has no
building scope = district-wide).

Revision ID: a054_cast_devices_alert_routing
Revises: a053_dhcp_population
Create Date: 2026-04-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a054_cast_devices_alert_routing"
down_revision: Union[str, None] = "a053_dhcp_population"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cast_devices",
        sa.Column(
            "severity_threshold",
            sa.String(20),
            nullable=False,
            server_default="critical",
        ),
    )
    op.add_column(
        "cast_devices",
        sa.Column("building_code", sa.String(20), nullable=True),
    )
    op.create_check_constraint(
        "cast_devices_severity_valid",
        "cast_devices",
        "severity_threshold IN ('critical','high','medium','info')",
    )
    # Existing rows default to "critical" via the server_default above —
    # the least-noisy choice. Users can loosen per-device in the UI.

    # auto_cast is folded into severity/building routing — drop it.
    op.drop_column("cast_devices", "auto_cast")


def downgrade() -> None:
    op.add_column(
        "cast_devices",
        sa.Column("auto_cast", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.drop_constraint("cast_devices_severity_valid", "cast_devices", type_="check")
    op.drop_column("cast_devices", "building_code")
    op.drop_column("cast_devices", "severity_threshold")
