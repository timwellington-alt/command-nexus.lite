"""Voice module tables: voice_recipients, voice_cdr, voice_auth_log.

Phase 12A schema. Per the Hard Boundary in
Project Specs/Voice Module/PHASE_12_VOICE_MODULE.md, these tables
have NO foreign keys to staff_directory, users, roster_snapshots, or
any people table. Voice is infrastructure-only — recipients are
dial targets, not Nexus user accounts.

INT primary keys throughout to match Nexus convention. No people FK,
no alerts FK (the alerts table doesn't exist in Nexus — dispatch job
passes source_module + source_ref text fields instead).

Also seeds the initial two recipients: the admin's mobile (Tim) and
the second tech staff mobile (Chance). Both default to severity
threshold 'critical' — they will only get voice calls for
critical-level alerts. Tweaking is done via the Settings UI or direct
UPDATE; the seed exists so the first alert-fire test has somewhere to
dial.

Revision ID: a041_voice_module
Revises: a040_net_alert_priority
Create Date: 2026-04-11
"""

from typing import Sequence, Union
from alembic import op

revision: str = "a041_voice_module"
down_revision: Union[str, None] = "a040_net_alert_priority"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_recipients (
            id SERIAL PRIMARY KEY,
            label VARCHAR(80) NOT NULL,
            number_e164 VARCHAR(20),
            extension VARCHAR(10),
            severity_threshold VARCHAR(20) NOT NULL DEFAULT 'critical',
            quiet_hours_start TIME,
            quiet_hours_end TIME,
            quiet_hours_override_severity VARCHAR(20),
            pin_hash VARCHAR(255),
            active BOOLEAN NOT NULL DEFAULT TRUE,
            priority_order INTEGER NOT NULL DEFAULT 100,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            CHECK (number_e164 IS NOT NULL OR extension IS NOT NULL),
            CHECK (severity_threshold IN ('critical','high','medium','info'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_voice_recipients_active "
        "ON voice_recipients (active, priority_order)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_cdr (
            id SERIAL PRIMARY KEY,
            call_id VARCHAR(100),
            direction VARCHAR(10) NOT NULL CHECK (direction IN ('inbound','outbound')),
            recipient_id INTEGER REFERENCES voice_recipients(id) ON DELETE SET NULL,
            callee VARCHAR(50),
            caller VARCHAR(50),
            source_module VARCHAR(50),
            source_ref VARCHAR(100),
            severity VARCHAR(20),
            auth_outcome VARCHAR(20),
            call_outcome VARCHAR(20),
            ack_dtmf VARCHAR(10),
            duration_seconds INTEGER,
            speech_text TEXT,
            started_at TIMESTAMPTZ DEFAULT NOW(),
            ended_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_voice_cdr_started "
        "ON voice_cdr (started_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_voice_cdr_source "
        "ON voice_cdr (source_module, source_ref)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_auth_log (
            id SERIAL PRIMARY KEY,
            number_e164 VARCHAR(20),
            event VARCHAR(30) NOT NULL,
            detail TEXT,
            occurred_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # Seed the initial two recipients. Idempotent via LABEL uniqueness check.
    # Both get the default quiet hours 22:00-06:00 with 'critical' override,
    # meaning: no voice calls overnight EXCEPT for critical severity alerts,
    # which always go through. Matches the user's 2026-04-12 directive.
    op.execute(
        """
        INSERT INTO voice_recipients
            (label, number_e164, severity_threshold, priority_order, active,
             quiet_hours_start, quiet_hours_end, quiet_hours_override_severity)
        SELECT 'Tim (admin)', '+17408211712', 'critical', 10, TRUE,
               '22:00', '06:00', 'critical'
        WHERE NOT EXISTS (SELECT 1 FROM voice_recipients WHERE label = 'Tim (admin)')
        """
    )
    op.execute(
        """
        INSERT INTO voice_recipients
            (label, number_e164, severity_threshold, priority_order, active,
             quiet_hours_start, quiet_hours_end, quiet_hours_override_severity)
        SELECT 'Chance (tech)', '+17409888243', 'critical', 20, TRUE,
               '22:00', '06:00', 'critical'
        WHERE NOT EXISTS (SELECT 1 FROM voice_recipients WHERE label = 'Chance (tech)')
        """
    )

    # ── Voice integration settings — default quiet hours ──────────
    # These are the DEFAULTS for new recipients (read by the Settings
    # page when adding a new recipient and by the future recipient-
    # create form as the pre-filled values). Per-recipient overrides
    # still live on voice_recipients itself. Centralizing the defaults
    # here means you change them once and future recipients inherit.
    op.execute(
        """
        INSERT INTO integration_configs (integration, key, value, is_secret_ref, updated_by)
        VALUES
            ('voice', 'default_quiet_hours_start',    '22:00',    FALSE, 'migration:a041'),
            ('voice', 'default_quiet_hours_end',      '06:00',    FALSE, 'migration:a041'),
            ('voice', 'default_quiet_hours_override', 'critical', FALSE, 'migration:a041'),
            ('voice', 'default_severity_threshold',   'critical', FALSE, 'migration:a041')
        ON CONFLICT (integration, key) DO NOTHING
        """
    )

    # ── Permissions ────────────────────────────────────────────────
    op.execute("""
        INSERT INTO permissions (action, description, is_student_sensitive)
        VALUES
            ('voice.view',     'View voice recipients + CDR',      false),
            ('voice.manage',   'Create/edit/delete voice recipients', false),
            ('voice.dispatch', 'Fire voice alerts (test + production)', false)
        ON CONFLICT (action) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r, permissions p
        WHERE r.name = 'admin'
          AND p.action IN ('voice.view', 'voice.manage', 'voice.dispatch')
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM integration_configs
        WHERE integration = 'voice'
          AND key IN (
            'default_quiet_hours_start',
            'default_quiet_hours_end',
            'default_quiet_hours_override',
            'default_severity_threshold'
          )
    """)
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions
            WHERE action IN ('voice.view','voice.manage','voice.dispatch')
        )
    """)
    op.execute("""
        DELETE FROM permissions
        WHERE action IN ('voice.view','voice.manage','voice.dispatch')
    """)
    op.execute("DROP TABLE IF EXISTS voice_auth_log")
    op.execute("DROP INDEX IF EXISTS ix_voice_cdr_source")
    op.execute("DROP INDEX IF EXISTS ix_voice_cdr_started")
    op.execute("DROP TABLE IF EXISTS voice_cdr")
    op.execute("DROP INDEX IF EXISTS ix_voice_recipients_active")
    op.execute("DROP TABLE IF EXISTS voice_recipients")
