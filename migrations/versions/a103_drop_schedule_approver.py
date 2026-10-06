"""Tickets module — drop the schedule_approver setting key.

Phase 3 cleanup: ``tickets.schedule_approver`` was a wart — the
routing-rules system already handles "who gets a schedule ticket"
via a rule {category: 'schedule', ...}. This drops the orphaned
key and (if it held a non-empty value) folds that value into
``tickets.routing_rules`` so no operator config is lost.

Revision ID: a103_drop_schedule_approver
Revises: a102_schedule_recurrence
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a103_drop_schedule_approver"
down_revision: Union[str, None] = "a102_schedule_recurrence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fold any configured value into routing_rules as a {category:
    # schedule} rule. Done via a PL/pgSQL block so we can parse the
    # existing JSON, prepend, and write back atomically.
    op.execute("""
    DO $$
    DECLARE
        approver TEXT;
        rules_json JSONB;
        new_rule JSONB;
    BEGIN
        SELECT TRIM(value) INTO approver
        FROM integration_configs
        WHERE integration='tickets' AND key='schedule_approver';

        IF approver IS NOT NULL AND approver <> '' THEN
            SELECT COALESCE(value::JSONB, '[]'::JSONB) INTO rules_json
            FROM integration_configs
            WHERE integration='tickets' AND key='routing_rules';

            new_rule := jsonb_build_object(
                'category', 'schedule',
                'sub_category', '',
                'building', '',
                'assignee_email', approver
            );

            -- Prepend so schedule routing wins over more-general rules
            -- if any operator later adds an ambiguous rule.
            rules_json := jsonb_build_array(new_rule) || COALESCE(rules_json, '[]'::JSONB);

            UPDATE integration_configs
            SET value = rules_json::TEXT, updated_at = NOW()
            WHERE integration='tickets' AND key='routing_rules';

            IF NOT FOUND THEN
                INSERT INTO integration_configs (integration, key, value, is_secret_ref)
                VALUES ('tickets', 'routing_rules', rules_json::TEXT, false);
            END IF;
        END IF;
    END $$;
    """)
    op.execute(
        "DELETE FROM integration_configs "
        "WHERE integration='tickets' AND key='schedule_approver'"
    )


def downgrade() -> None:
    # Re-create the (empty) setting key. Folded-into-routing-rules data
    # is left in place — it's where it belongs now.
    op.execute(
        "INSERT INTO integration_configs (integration, key, value, is_secret_ref) "
        "VALUES ('tickets', 'schedule_approver', '', false) "
        "ON CONFLICT (integration, key) DO NOTHING"
    )
