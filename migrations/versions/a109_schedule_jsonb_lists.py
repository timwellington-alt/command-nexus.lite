"""Schedule: convert services_needed/invitees/repeat_days Text/JSON → JSONB.

Three columns on ``ticket_schedule`` stored JSON-encoded lists as
Text. Reads round-tripped through ``json.loads`` on every request
and queries couldn't filter by list contents without a Python pass.

This migration switches all three to JSONB. The data in those Text
columns is already valid JSON (the writes used ``json.dumps``), so
the cast `column::jsonb` lifts every existing row cleanly. Defaults
are switched from `'[]'` (text) to `'[]'::jsonb`.

After this migration:
- `SELECT ... WHERE 'custodial' = ANY(SELECT jsonb_array_elements_text(services_needed))`
  works without round-tripping through Python
- Reports can aggregate on services_needed in SQL
- The model gains list-typed access (no `json.loads` on read)

Revision ID: a109_schedule_jsonb_lists
Revises: a108_ticket_facility_roles
Create Date: 2026-05-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "a109_schedule_jsonb_lists"
down_revision: Union[str, None] = "a108_ticket_facility_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # services_needed (NOT NULL, default '[]')
    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN services_needed DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN services_needed TYPE jsonb "
        "USING services_needed::jsonb"
    )
    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN services_needed SET DEFAULT '[]'::jsonb"
    )

    # invitees (NOT NULL, default '[]')
    op.execute("ALTER TABLE ticket_schedule ALTER COLUMN invitees DROP DEFAULT")
    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN invitees TYPE jsonb USING invitees::jsonb"
    )
    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN invitees SET DEFAULT '[]'::jsonb"
    )

    # repeat_days (nullable, no default — NULL → NULL is fine)
    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN repeat_days TYPE jsonb "
        "USING CASE WHEN repeat_days IS NULL THEN NULL ELSE repeat_days::jsonb END"
    )


def downgrade() -> None:
    # Cast back to text. JSONB → text yields valid JSON, so re-reads
    # via the prior code path (json.loads) work without data loss.
    op.execute("ALTER TABLE ticket_schedule ALTER COLUMN services_needed DROP DEFAULT")
    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN services_needed TYPE text USING services_needed::text"
    )
    op.execute("ALTER TABLE ticket_schedule ALTER COLUMN services_needed SET DEFAULT '[]'")

    op.execute("ALTER TABLE ticket_schedule ALTER COLUMN invitees DROP DEFAULT")
    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN invitees TYPE text USING invitees::text"
    )
    op.execute("ALTER TABLE ticket_schedule ALTER COLUMN invitees SET DEFAULT '[]'")

    op.execute(
        "ALTER TABLE ticket_schedule "
        "ALTER COLUMN repeat_days TYPE text "
        "USING CASE WHEN repeat_days IS NULL THEN NULL ELSE repeat_days::text END"
    )
