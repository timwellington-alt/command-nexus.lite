"""Split guidance auto-queue into separate knobs.

The old `guidance.auto_queue_enabled` setting gated two unrelated
concerns:

    1. Whether new enrollments / withdrawals get inserted into the
       guidance queue
    2. Whether counselor groups get email notifications when new
       queue entries land

Both were bundled under one flag, which meant "I want the counselors
to stop getting emails" required turning off the queue entirely, and
"I want to start filling the queue" meant counselors immediately
started getting emails. Decoupling them lets the operator start
populating the queue safely (backed by the new
`guidance.auto_queue_recent_days` window so no avalanche) while
leaving counselor emails off until they're explicitly ready.

This migration:
  - Copies any existing value from `guidance.auto_queue_enabled` to
    the new `guidance.notify_enabled` key
  - Deletes the old `auto_queue_enabled` row
  - Seeds `guidance.auto_queue_recent_days` to "3" if not already set

Queue insertion itself no longer consults any flag — it's always on,
scoped by `auto_queue_recent_days`. Automated Google account creation
remains gated by the pre-existing `roster.student_google_writes_enabled`
flag and is NOT touched by this migration.

Revision ID: a037_guidance_notify
Revises: a036_transport_view
Create Date: 2026-04-10
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a037_guidance_notify"
down_revision: Union[str, None] = "a036_transport_view"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Copy the old value to the new key if the old row exists and the
    # new row doesn't. Idempotent — re-running won't clobber a manual
    # edit to notify_enabled that may have happened after the migration.
    existing_new = conn.execute(sa.text(
        "SELECT id FROM integration_configs "
        "WHERE integration='guidance' AND key='notify_enabled'"
    )).fetchone()

    old_row = conn.execute(sa.text(
        "SELECT value, is_secret_ref, updated_by FROM integration_configs "
        "WHERE integration='guidance' AND key='auto_queue_enabled'"
    )).fetchone()

    if not existing_new and old_row:
        conn.execute(sa.text(
            "INSERT INTO integration_configs "
            "(integration, key, value, is_secret_ref, updated_by, updated_at) "
            "VALUES ('guidance', 'notify_enabled', :val, :sec, :who, NOW())"
        ), {
            "val": old_row[0],
            "sec": old_row[1] or False,
            "who": "a037_migration",
        })

    # Delete the old key regardless — having both around causes confusion.
    conn.execute(sa.text(
        "DELETE FROM integration_configs "
        "WHERE integration='guidance' AND key='auto_queue_enabled'"
    ))

    # Seed auto_queue_recent_days=3 if not already present.
    existing_days = conn.execute(sa.text(
        "SELECT id FROM integration_configs "
        "WHERE integration='guidance' AND key='auto_queue_recent_days'"
    )).fetchone()
    if not existing_days:
        conn.execute(sa.text(
            "INSERT INTO integration_configs "
            "(integration, key, value, is_secret_ref, updated_by, updated_at) "
            "VALUES ('guidance', 'auto_queue_recent_days', '3', false, 'a037_migration', NOW())"
        ))


def downgrade() -> None:
    conn = op.get_bind()

    # Restore the old key from the new one if needed.
    new_row = conn.execute(sa.text(
        "SELECT value, is_secret_ref, updated_by FROM integration_configs "
        "WHERE integration='guidance' AND key='notify_enabled'"
    )).fetchone()

    old_exists = conn.execute(sa.text(
        "SELECT id FROM integration_configs "
        "WHERE integration='guidance' AND key='auto_queue_enabled'"
    )).fetchone()

    if new_row and not old_exists:
        conn.execute(sa.text(
            "INSERT INTO integration_configs "
            "(integration, key, value, is_secret_ref, updated_by, updated_at) "
            "VALUES ('guidance', 'auto_queue_enabled', :val, :sec, :who, NOW())"
        ), {"val": new_row[0], "sec": new_row[1] or False, "who": "a037_downgrade"})

    conn.execute(sa.text(
        "DELETE FROM integration_configs "
        "WHERE integration='guidance' AND key IN ('notify_enabled', 'auto_queue_recent_days')"
    ))
