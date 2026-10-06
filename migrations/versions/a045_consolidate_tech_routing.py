"""Consolidate tech routing into voice_recipients.

Adds mac_address / home_lat / home_lon directly to voice_recipients.
Re-keys tech_push_subscriptions from tech_team → voice_recipients.
Drops the now-redundant tech_team table.

Revision ID: a045_consolidate_tech_routing
Revises: a044_tech_routing
Create Date: 2026-04-16
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a045_consolidate_tech_routing"
down_revision: Union[str, None] = "a044_tech_routing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add location fields to voice_recipients
    op.add_column("voice_recipients", sa.Column("mac_address", sa.String(20), nullable=True))
    op.add_column("voice_recipients", sa.Column("home_lat", sa.Float, nullable=True))
    op.add_column("voice_recipients", sa.Column("home_lon", sa.Float, nullable=True))

    # Rebuild tech_push_subscriptions: drop tech_id FK, add recipient_id FK
    op.drop_index("ix_tech_push_subs_tech_id", table_name="tech_push_subscriptions")
    op.drop_constraint("tech_push_subscriptions_tech_id_fkey", "tech_push_subscriptions", type_="foreignkey")
    op.alter_column("tech_push_subscriptions", "tech_id", new_column_name="recipient_id")
    op.create_foreign_key(
        "tech_push_subscriptions_recipient_id_fkey",
        "tech_push_subscriptions", "voice_recipients",
        ["recipient_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_tech_push_subs_recipient_id", "tech_push_subscriptions", ["recipient_id"])

    # Drop the tech_team table (no data — was just created in a044)
    op.drop_table("tech_team")


def downgrade() -> None:
    # Recreate tech_team
    op.create_table(
        "tech_team",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("phone_ext", sa.String(20)),
        sa.Column("phone_mobile", sa.String(20)),
        sa.Column("mac_address", sa.String(20)),
        sa.Column("home_lat", sa.Float),
        sa.Column("home_lon", sa.Float),
        sa.Column("priority_order", sa.Integer, nullable=False),
        sa.Column("voice_recipient_id", sa.Integer, sa.ForeignKey("voice_recipients.id")),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Revert push subscriptions
    op.drop_index("ix_tech_push_subs_recipient_id", table_name="tech_push_subscriptions")
    op.drop_constraint("tech_push_subscriptions_recipient_id_fkey", "tech_push_subscriptions", type_="foreignkey")
    op.alter_column("tech_push_subscriptions", "recipient_id", new_column_name="tech_id")
    op.create_foreign_key(
        "tech_push_subscriptions_tech_id_fkey",
        "tech_push_subscriptions", "tech_team",
        ["tech_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_tech_push_subs_tech_id", "tech_push_subscriptions", ["tech_id"])

    # Drop added columns
    op.drop_column("voice_recipients", "mac_address")
    op.drop_column("voice_recipients", "home_lat")
    op.drop_column("voice_recipients", "home_lon")
