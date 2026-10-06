"""Cross-reference Paxton API names into paxton_acu_status.

Net2 REST API exposes a /doors list with names like "1G Wing (Door 4)".
Each door.id is the ACU serial, which is the last 24 bits of the
00:0b:d6 MAC. We cache the name (and the door_id for reference) so the
Network page Access dots can show a meaningful label instead of just a
MAC address.

Revision ID: a121_paxton_acu_name
Revises: a120_paxton_acu_status
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a121_paxton_acu_name"
down_revision = "a120_paxton_acu_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("paxton_acu_status") as b:
        b.add_column(sa.Column("name", sa.String(255), nullable=True))
        b.add_column(sa.Column("door_id", sa.BigInteger, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("paxton_acu_status") as b:
        b.drop_column("door_id")
        b.drop_column("name")
