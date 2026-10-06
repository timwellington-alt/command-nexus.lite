"""Building floorplan registry + ingest-wizard state.

Adds `building_floorplans` — one row per building under
`data/building_floor_plans/<code>/`. Holds the wizard's session state
between steps (uploads on disk, slot mapping, layer overrides, anchors,
outline) so the operator can leave the wizard and resume later.

The final per-floor outputs still live in
`data/building_floor_plans/<code>/<floor>/manifest.json` — that's the
runtime source of truth. This table is for the wizard's bookkeeping
plus the list-page registry.

Backfills four rows from `DISTINCT building_code` in `facility_rooms`
(AC, EPE, PES, PHS). EPE + PES get `ingest_status='complete'` since
they're already shipped; AC + PHS get `'none'` since no floor plan
exists yet.

Revision ID: a111_building_floorplans
Revises: a110_chat_history_sanitize
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a111_building_floorplans"
down_revision = "a110_chat_history_sanitize"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "building_floorplans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("building_code", sa.String(20), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("floor_count", sa.Integer, nullable=False, server_default="1"),
        # none — no DXFs ingested yet (placeholder building)
        # draft — building created, wizard not started
        # in_progress — wizard partially completed
        # complete — at least one floor fully rendered + manifest written
        sa.Column("ingest_status", sa.String(20), nullable=False, server_default="none"),
        sa.Column("current_step", sa.String(40), nullable=True),
        # Wizard state: uploaded file paths, slot→DXF mapping, layer overrides,
        # anchors-in-progress, outline-in-progress, per-floor progress
        sa.Column(
            "wizard_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_building_floorplans_status",
        "building_floorplans",
        ["ingest_status"],
    )

    # Backfill from existing buildings observable in facility_rooms.
    # Display names match the human-facing names we already use elsewhere;
    # operator can edit them in the wizard.
    op.execute("""
        INSERT INTO building_floorplans
            (building_code, display_name, floor_count, ingest_status)
        VALUES
            ('AC',  'Administration Center',         1, 'none'),
            ('EPE', 'Elementary School 1',    1, 'complete'),
            ('PES', 'Elementary School 2',  3, 'complete'),
            ('PHS', 'High School',        1, 'none')
        ON CONFLICT (building_code) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_index("ix_building_floorplans_status", table_name="building_floorplans")
    op.drop_table("building_floorplans")
