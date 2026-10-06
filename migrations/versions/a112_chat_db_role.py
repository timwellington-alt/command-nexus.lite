"""Create `nexus_chat` Postgres role for the chat agent's read-only
DB tool to SET LOCAL ROLE into before running operator queries.

The chat module's DB tool refuses to execute SELECTs when the active
DB role is a superuser — because superusers can call
pg_read_file() / pg_ls_dir() inside a SELECT to leak host files,
which `transaction_read_only = on` does NOT block.

The app currently connects as `nexus_admin` (which is a superuser),
so chat queries always abort. Fix: create a non-superuser role,
grant the app role membership in it, and have the chat tool
`SET LOCAL ROLE nexus_chat` inside its transaction before checking
`is_superuser`.

Role design:
- NOLOGIN — never connected to directly; only reachable via SET ROLE
- NOINHERIT — privileges only apply when explicitly switched into
- Granted `pg_read_all_data` — Postgres-built-in role giving SELECT
  on every table without per-table grants

Revision ID: a112_chat_db_role
Revises: a111_building_floorplans
"""
from __future__ import annotations

from alembic import op


revision = "a112_chat_db_role"
down_revision = "a111_building_floorplans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CREATE ROLE is not transactional in all Postgres versions, so
    # guard with a DO block that no-ops if the role already exists.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_chat') THEN
                CREATE ROLE nexus_chat NOLOGIN NOINHERIT;
            END IF;
        END$$;
    """)
    op.execute("GRANT pg_read_all_data TO nexus_chat;")
    # Allow nexus_admin (the app's connection role) to SET ROLE nexus_chat.
    op.execute("GRANT nexus_chat TO nexus_admin;")


def downgrade() -> None:
    op.execute("REVOKE nexus_chat FROM nexus_admin;")
    op.execute("REVOKE pg_read_all_data FROM nexus_chat;")
    op.execute("DROP ROLE IF EXISTS nexus_chat;")
