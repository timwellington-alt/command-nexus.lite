"""Track DVR protocol version + firmware identity on bus_dvrs.

Different REI DVR generations speak materially different protocols on
port 9006:
    PRO 1.0.4 → HD5-600 (DSNO prefix 0086, MCU T21091301, MTYPE 26)
    PRO 1.0.6 → HD6N-1200 (DSNO prefix 0081, MCU HD6N-M02-…, MTYPE 188)

REQUESTDOWNLOADVIDEO works on 1.0.6 but returns 'NO TASK / ERRORCODE 40'
on 1.0.4 — the operation was added in the newer firmware. The adapter
needs to dispatch by version, so we record it on the bus row at login.

Revision ID: a093_bus_dvr_protocol
Revises: a092_footage_progress
Create Date: 2026-05-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a093_bus_dvr_protocol"
down_revision: Union[str, None] = "a092_footage_progress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bus_dvrs",
        sa.Column("protocol_version", sa.String(20), nullable=True),
    )
    op.add_column(
        "bus_dvrs",
        sa.Column("man_version", sa.String(40), nullable=True),
    )
    op.add_column(
        "bus_dvrs",
        sa.Column("mcu_version", sa.String(60), nullable=True),
    )
    op.add_column(
        "bus_dvrs",
        sa.Column("device_type", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bus_dvrs", "device_type")
    op.drop_column("bus_dvrs", "mcu_version")
    op.drop_column("bus_dvrs", "man_version")
    op.drop_column("bus_dvrs", "protocol_version")
