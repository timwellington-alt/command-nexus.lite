"""Per-source-module channel policy for routed alerts.

Global fanout has been on/off per-recipient and per-severity only —
no way to say "for observability alerts, push+email but never call."
Adds a small overrides table so operators can silence a class of
alert (or elevate it) without touching every recipient row.

``dispatch_routed_alert`` looks up the row for its incoming
``source_module`` and short-circuits disabled channels before the
per-recipient logic runs. Modules with no row inherit the previous
behavior (all channels considered, per-recipient rules apply).

Revision ID: a173_alert_module_overrides
Revises: a172_job_health
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "a173_alert_module_overrides"
down_revision = "a172_job_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_module_overrides",
        sa.Column("source_module", sa.String(60), primary_key=True),
        sa.Column("push_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("voice_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("cast_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("email_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        # NULL = respect recipient's per-severity gate. Otherwise a
        # module-wide floor: voice only fires when alert severity >=
        # this value.
        sa.Column("min_severity_voice", sa.String(20)),
        # When true, quiet-hours suppression is skipped for this module
        # (e.g. HALO safety alerts fire 24/7).
        sa.Column("quiet_hours_bypass", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text),
        sa.Column("updated_by", sa.String(255)),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )

    # Seed one row so this feature has an immediately-useful default —
    # observability (the job_health / worker-liveness class) goes to
    # push + email only. Tim asked for this on 2026-08-20 after the
    # cold-start voice flood.
    op.execute("""
        INSERT INTO alert_module_overrides
            (source_module, push_enabled, voice_enabled, cast_enabled,
             email_enabled, min_severity_voice, quiet_hours_bypass, notes,
             updated_by, updated_at)
        VALUES
            ('observability', TRUE, FALSE, FALSE, TRUE,
             NULL, FALSE,
             'Job liveness / worker health alerts — push + email only. '
             'Not important enough to page the phone tree.',
             'system', NOW())
    """)


def downgrade() -> None:
    op.drop_table("alert_module_overrides")
