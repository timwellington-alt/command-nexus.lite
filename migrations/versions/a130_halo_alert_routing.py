"""HALO alert routing — recipients, push subscriptions, delivery log.

HALO event alerts (Vape, THC, Gunshot, etc.) are SEPARATE from the
existing tech/maintenance alert routing. They go to school admin, SROs,
counselors — not the IT team. Distinct recipient table, distinct
delivery log, distinct settings UI.

Three channels supported in v1:
  - voice   — POST direct to nexus-voice container (bypasses
              VoiceRecipient PII coupling; only phone + TTS text leave
              the main schema)
  - email   — via app.workers.notifications._send_email
  - push    — VAPID web push to subscribed mobile devices

Per-recipient: severity_min, quiet_hours, channel preferences, building
scope (null = district-wide).

Revision ID: a130_halo_alert_routing
Revises: a129_halo_integration
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a130_halo_alert_routing"
down_revision = "a129_halo_integration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "halo_alert_recipients",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("title", sa.String(120), nullable=True),
        sa.Column("building", sa.String(50), nullable=True),  # NULL = district-wide
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone_e164", sa.String(20), nullable=True),
        sa.Column("extension", sa.String(10), nullable=True),
        sa.Column("severity_min", sa.String(20), nullable=False, server_default="critical"),
        # channels stored as comma-separated for easy editing in Settings UI
        # (voice,email,push) — empty means "use all that have a target"
        sa.Column("channels", sa.String(80), nullable=False, server_default="voice,email,push"),
        sa.Column("quiet_hours_start", sa.Time, nullable=True),
        sa.Column("quiet_hours_end", sa.Time, nullable=True),
        sa.Column("override_severity", sa.String(20), nullable=True),  # always-deliver tier (e.g. critical)
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_halo_alert_recipients_building",
                    "halo_alert_recipients", ["building"])
    op.create_index("ix_halo_alert_recipients_enabled",
                    "halo_alert_recipients", ["enabled"])

    op.create_table(
        "halo_recipient_push_subscriptions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("recipient_id", sa.Integer,
                  sa.ForeignKey("halo_alert_recipients.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("p256dh", sa.String(255), nullable=False),
        sa.Column("auth", sa.String(255), nullable=False),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("recipient_id", "endpoint", name="uq_halo_push_recipient_endpoint"),
    )

    op.create_table(
        "halo_alert_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger,
                  sa.ForeignKey("halo_events.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("recipient_id", sa.Integer,
                  sa.ForeignKey("halo_alert_recipients.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("recipient_label", sa.String(120), nullable=True),  # snapshot for after-deletes
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),  # sent / failed / skipped / quiet_hours
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_halo_alert_log_event", "halo_alert_log", ["event_id"])
    op.create_index("ix_halo_alert_log_attempted", "halo_alert_log", ["attempted_at"])


def downgrade() -> None:
    op.drop_table("halo_alert_log")
    op.drop_table("halo_recipient_push_subscriptions")
    op.drop_table("halo_alert_recipients")
