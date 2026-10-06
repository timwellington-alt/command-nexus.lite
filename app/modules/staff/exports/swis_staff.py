"""SWIS PBIS Person Import — Staff CSV, split per building.

Spec: /Project Specs/SWISPBIS/SWIS-Suite-Person-Import-Specification.pdf

SWIS is a per-school subscription — each building has its own account and
its own upload. So we emit one file per teaching building rather than a
combined roster. Teachers who work at more than one building appear in
each of that building's files (correct behavior — SWIS uses the
staffDistrictId to dedup across schools).

Emitted columns (in order): staffDistrictId, firstName, lastName, email, status

Design choices:
  - One exporter per building present in `student_teachers`, registered
    at import time. Building display names come from the
    `branding.school_names` setting; falls back to the raw code.
  - Only exports staff who currently have at least one section
    assignment in `student_teachers` FOR THAT BUILDING. Filters out
    non-teaching staff (custodial, admin, support) and service accounts.
  - staffDistrictId = email. Nexus has no universal district employee
    ID stored for staff. Email is stable, unique, and covers everyone.
  - Rows without email OR without first/last name are excluded.
  - Archived staff excluded (SWIS spec: archived records ignored on import).
  - status mapping: active/on-leave=1, inactive=2.
"""
import json
import logging
from typing import AsyncIterator

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from .base import Exporter, register, register_discovery


logger = logging.getLogger(__name__)


def _make_generator(building_code: str):
    """Return an async generator scoped to one building's teachers."""

    async def _generate(db: AsyncSession) -> AsyncIterator[list[str]]:
        yield ["staffDistrictId", "firstName", "lastName", "email", "status"]

        rows = (await db.execute(sa_text("""
            SELECT sd.email, sd.first_name, sd.last_name, sd.status
            FROM staff_directory sd
            WHERE sd.status IN ('active', 'inactive')
              AND sd.email IS NOT NULL AND sd.email <> ''
              AND sd.first_name IS NOT NULL AND sd.first_name <> ''
              AND sd.last_name IS NOT NULL AND sd.last_name <> ''
              AND LOWER(sd.email) IN (
                SELECT DISTINCT LOWER(teacher_email)
                FROM student_teachers
                WHERE teacher_email IS NOT NULL AND teacher_email <> ''
                  AND school = :building
              )
            ORDER BY sd.last_name, sd.first_name
        """).bindparams(building=building_code))).mappings().all()

        seen_emails: set[str] = set()
        for r in rows:
            email = (r["email"] or "").strip().lower()
            if not email or email in seen_emails:
                continue
            seen_emails.add(email)

            status_id = "2" if (r["status"] or "").lower() == "inactive" else "1"
            yield [
                email,
                (r["first_name"] or "").strip(),
                (r["last_name"] or "").strip(),
                email,
                status_id,
            ]

    return _generate


async def _discover(db: AsyncSession) -> None:
    """Query student_teachers for distinct teaching buildings, look up
    display names from settings, and register one Exporter per building.
    Runs lazily on first catalog access (see base.ensure_discovered)."""
    buildings = [r[0] for r in (await db.execute(sa_text("""
        SELECT DISTINCT school FROM student_teachers
        WHERE school IS NOT NULL AND school <> ''
        ORDER BY school
    """))).all()]

    school_names: dict[str, str] = {}
    try:
        raw = (await db.execute(sa_text(
            "SELECT value FROM integration_configs "
            "WHERE integration='branding' AND key='school_names'"
        ))).scalar_one_or_none()
        if raw:
            school_names = json.loads(raw)
    except Exception as e:
        logger.warning(f"swis_staff: school_names lookup failed: {e}")

    for code in buildings:
        display = school_names.get(code, code)
        register(Exporter(
            id=f"swis-staff-{code.lower()}",
            name=f"SWIS Staff Import — {display}",
            for_app="SWIS PBIS Person Import",
            description=(
                f"Teachers assigned to at least one section at {display} "
                f"({code}). SWIS is a per-school subscription — upload this "
                f"file to that building's SWIS account. Teachers who work "
                f"in multiple buildings appear in each building's file."
            ),
            filename_pattern=f"swis-staff-{code.lower()}-{{yyyymmdd}}.csv",
            generator=_make_generator(code),
            tags=["swis", "pbis", "staff", code.lower()],
        ))


# Registered at import time; the router calls ensure_discovered(db)
# before rendering the catalog, which invokes this once per process.
register_discovery(_discover)
