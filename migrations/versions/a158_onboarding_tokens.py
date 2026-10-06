"""Self-service onboarding tokens.

Admin generates a signed single-use URL scoped to a specific building.
The recipient (a new hire) fills in their own info via
``/onboard/{token}``; submit writes the row to that building's
``NexusData`` room-roster tab AND inserts a ``staff_queue`` row with
status ``pending_review`` so provisioning is admin-gated.

The token IS the auth — no login required at the recipient's end —
so short TTL + single-use + audit are the safety rails.

Revision ID: a158_onboarding_tokens
Revises: a157_lightning_strikes
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a158_onboarding_tokens"
down_revision = "a157_lightning_strikes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_tokens",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # secrets.token_urlsafe(32) — 43 chars, url-safe base64. Unique.
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        # Which building's NexusData tab this token writes into. Matches
        # the SIS-facing codes used in room_roster.buildings config
        # (SIS codes) OR the internal codes — whichever
        # the issuer picked. Resolved at submit-time via the same
        # sis_to_internal map the roster sync uses.
        sa.Column("building", sa.String(20), nullable=False),
        sa.Column("issued_by", sa.String(255), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Default single-use; multi-use kept as a schema option for a
        # future "school-year mass onboarding" flow that hands the same
        # link to a whole cohort.
        sa.Column("single_use", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        # Optional pre-fill hints the issuer can seed. Rendered in the
        # form as pre-populated fields the user can still edit.
        sa.Column("preset_room", sa.String(50)),
        sa.Column("preset_title", sa.String(255)),
        sa.Column("notes", sa.Text),
        # ── State ──
        # status transitions:
        #   pending  → submitted (recipient POSTs valid data)
        #            → expired   (nightly cron, expires_at < now)
        #            → revoked   (admin manually kills it)
        sa.Column("status", sa.String(20), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        # What the recipient submitted — persisted verbatim for audit
        # even if the resulting_queue_id row later gets purged. Includes
        # ip + user_agent for forensics.
        sa.Column("submitted_data", postgresql.JSONB),
        sa.Column("resulting_queue_id", sa.Integer),
    )
    op.create_index(
        "ix_onboarding_tokens_token",
        "onboarding_tokens", ["token"], unique=True,
    )
    # Expiry sweep query — pending tokens whose expires_at is past.
    op.create_index(
        "ix_onboarding_tokens_status_expires",
        "onboarding_tokens", ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_onboarding_tokens_status_expires", table_name="onboarding_tokens")
    op.drop_index("ix_onboarding_tokens_token", table_name="onboarding_tokens")
    op.drop_table("onboarding_tokens")
