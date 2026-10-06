"""Add alert_priority to network_device_cache + topology-based defaults.

Phase 12A milestone 3 prep (2026-04-11). Adds a per-switch alert
priority that the voice dispatch worker uses to decide whether a
switch-down event should fire a voice call, an email, or just
update the dashboard.

Priority scale:
    critical  — building-killing or district-killing. Voice call any
                hour, any recipient whose severity_threshold allows it.
                Kills a building or crosses the district boundary if
                down. Examples: CO uplinks, building main-closet
                aggregation, named ISP/meta uplinks, fiber distribution
                chassis.
    high      — important building infrastructure below core. Voice
                during operating hours, email otherwise. Examples:
                main-closet access/PoE, closet aggregation 2930s.
    medium    — classroom/hallway access. Email + dashboard, never
                voice. Default for unknown devices.
    info      — non-critical, seasonal, isolated, or test. Dashboard
                only. Examples: athletic complex, maintenance,
                library legacy, test benches.

Topology heuristic used for the initial UPDATE:

    critical:
      - any sysname containing 'meta-link', 'to-meta', 'to-de'
      - 'mc-6200-fiber' (the 6200yl distribution chassis at PES)
      - every switch in the CO (Central Office) building — district HQ
      - '{pes|phs|epe}-mc-2930*' — building main-closet aggregation

    high:
      - other '{pes|phs|epe}-mc-*' main closet switches (NOT testbench)
      - '{pes|phs|epe}-tr1b-2930' / '-tc1b-2930f' closet aggregation
      - EXCEPT: '*testbench*' downgraded to info

    info:
      - every switch in the AC (Athletic Complex) building
      - MAINT building
      - '*testbench*' anywhere
      - '*library*2824' (isolated legacy library switch)

    medium:
      - every switch in PES, PHS, EPE not matched above
      - unassigned / unknown buildings (tlc2, blank)

The heuristic is driven by sysname patterns so it's reproducible
against any future switch that inherits the same naming convention.
Applied once at migration time; after that, the column is the source
of truth and sysname changes don't re-trigger the heuristic.

Revision ID: a040_net_alert_priority
Revises: a039_room_roster_cache
Create Date: 2026-04-11
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a040_net_alert_priority"
down_revision: Union[str, None] = "a039_room_roster_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add column with default 'medium' — safe across all existing rows
    op.execute(
        """
        ALTER TABLE network_device_cache
        ADD COLUMN IF NOT EXISTS alert_priority VARCHAR(20) NOT NULL DEFAULT 'medium'
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_network_device_cache_alert_priority "
        "ON network_device_cache (alert_priority)"
    )

    # CRITICAL — district core, named uplinks, primary building aggregation
    #
    # The -mc-2930 match is intentionally regex-constrained to the BASE
    # form (pes-mc-2930, phs-mc-2930) and the -2930f variant (epe-mc-2930f)
    # so NUMBERED auxiliary main-closet switches like `phs-mc-2930-10`
    # fall OUT of critical and land in high instead. 2030-04-12 user
    # review: phs-mc-2930-10 is an auxiliary, not primary aggregation.
    op.execute(
        """
        UPDATE network_device_cache
        SET alert_priority = 'critical'
        WHERE device_type = 'network'
          AND (
               LOWER(sysname) LIKE '%meta-link%'
            OR LOWER(sysname) LIKE '%to-meta%'
            OR LOWER(sysname) LIKE '%to-de%'
            OR LOWER(sysname) LIKE '%mc-6200-fiber%'
            OR building = 'CO'
            OR (
                building IN ('PES', 'PHS', 'EPE')
                AND LOWER(sysname) ~ '-mc-2930(f[^-]*)?$'
            )
          )
        """
    )

    # HIGH — other main closet switches + closet aggregation 2930s
    # Excludes testbench (handled in INFO below) and anything already critical.
    op.execute(
        """
        UPDATE network_device_cache
        SET alert_priority = 'high'
        WHERE device_type = 'network'
          AND alert_priority = 'medium'
          AND building IN ('PES', 'PHS', 'EPE')
          AND LOWER(sysname) NOT LIKE '%testbench%'
          AND (
               LOWER(sysname) LIKE '%-mc-%'
            OR LOWER(sysname) LIKE '%-tr1b-2930%'
            OR LOWER(sysname) LIKE '%-tc1b-2930f%'
          )
        """
    )

    # INFO — athletic, maintenance, test, legacy isolated
    op.execute(
        """
        UPDATE network_device_cache
        SET alert_priority = 'info'
        WHERE device_type = 'network'
          AND (
               building = 'AC'
            OR building = 'MAINT'
            OR LOWER(sysname) LIKE '%testbench%'
            OR LOWER(sysname) LIKE '%library%2824%'
          )
        """
    )

    # Everything else stays at the 'medium' default set by the column DEFAULT.


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_network_device_cache_alert_priority")
    op.execute("ALTER TABLE network_device_cache DROP COLUMN IF EXISTS alert_priority")
