"""SWIS PBIS Person Import — Student CSV, split per building.

Spec: /Project Specs/SWISPBIS/SWIS-Suite-Person-Import-Specification.pdf

SWIS is a per-school subscription — each building has its own account
and its own upload. Emits one file per school found in
`roster_snapshots`. Kids only attend one building at a time, so no
cross-building dedup is needed (unlike the staff exporter).

Emitted columns:
  studentDistrictId (Y), firstName (Y), lastName (Y), genderId (Y),
  gradeId (Y), hasIEP (N), race (N), status (N), isHispanic (N),
  isLanguageLearner (N)

Not emitted (not tracked in our data):
  has504, disability

Design choices:
  - `gender IS NOT NULL` filter naturally excludes the legacy-leak rows
    that never had the SWIS demographic columns populated. Once the
    legacy leak cleanup runs and those rows go inactive, the filter is
    still correct.
  - Multi-Racial (Race='M') → emitted with empty race field. SWIS has no
    "multi" ID; blank is the least-wrong answer without a source-side
    breakdown of which races.
  - Grade 'PS' (pre-school) → SWIS 14 (PK). Grade '23' (invalid) →
    emitted with empty gradeId — SWIS will reject.
  - status only emits 1 (active) or 2 (inactive). Archived rows are
    filtered out entirely per SWIS spec.
"""
import json
import logging
from typing import AsyncIterator

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from .base import Exporter, register, register_discovery


logger = logging.getLogger(__name__)


# ── Value mappers ────────────────────────────────────────────────────────

_GENDER_MAP = {
    "M": "1", "MALE": "1", "1": "1",
    "F": "2", "FEMALE": "2", "2": "2",
    "X": "3", "OTHER": "3", "3": "3",
}

# Single-letter EMIS race code → SWIS numeric ID.
# 'M' (multi-racial) intentionally not mapped — see module docstring.
_RACE_MAP = {
    "W": "5",   # White
    "B": "4",   # Black
    "A": "2",   # Asian
    "I": "1",   # American Indian/Alaskan Native
    "P": "8",   # Pacific Islander/Native Hawaiian
    "H": "3",   # Hispanic/Latinx  (SWIS treats race=3 as Hispanic)
}

_GRADE_MAP = {
    "01": "1", "1": "1",
    "02": "2", "2": "2",
    "03": "3", "3": "3",
    "04": "4", "4": "4",
    "05": "5", "5": "5",
    "06": "6", "6": "6",
    "07": "7", "7": "7",
    "08": "8", "8": "8",
    "09": "9", "9": "9",
    "10": "10",
    "11": "11",
    "12": "12",
    "KG": "15", "K": "15",
    "PK": "14",
    "PS": "14",   # Ohio SIS "Pre-School" — treat as SWIS PK
    "PKA": "16",
    "PKB": "17",
    "P12": "13",
}

_STATUS_MAP = {
    "active":   "1",
    "inactive": "2",
}


def _yn(v: str) -> str:
    """MetaSolutions Y/N → SWIS 1/0 (empty stays empty)."""
    v = (v or "").strip().upper()
    if v in ("Y", "YES", "1", "TRUE", "T"):
        return "1"
    if v in ("N", "NO", "0", "FALSE", "F"):
        return "0"
    return ""


def _make_generator(building_code: str):
    """Return an async generator scoped to one school's students."""

    async def _generate(db: AsyncSession) -> AsyncIterator[list[str]]:
        yield [
            "studentDistrictId", "firstName", "lastName", "genderId", "gradeId",
            "hasIEP", "race", "status", "isHispanic", "isLanguageLearner",
        ]

        rows = (await db.execute(sa_text("""
            SELECT sis_id, first_name, last_name, grade, status,
                   gender, race, hispanic_latino, ell_status, iep_status
            FROM roster_snapshots
            WHERE status IN ('active', 'inactive')
              AND gender IS NOT NULL AND gender <> ''
              AND school = :building
            ORDER BY last_name, first_name
        """).bindparams(building=building_code))).mappings().all()

        for r in rows:
            gender_raw = (r["gender"] or "").strip().upper()
            grade_raw  = (r["grade"] or "").strip().upper()
            race_raw   = (r["race"] or "").strip().upper()

            yield [
                (r["sis_id"] or "").strip(),
                (r["first_name"] or "").strip(),
                (r["last_name"] or "").strip(),
                _GENDER_MAP.get(gender_raw, ""),
                _GRADE_MAP.get(grade_raw, ""),
                _yn(r["iep_status"]),
                _RACE_MAP.get(race_raw, ""),   # 'M' + unknown → empty
                _STATUS_MAP.get((r["status"] or "").strip().lower(), "1"),
                _yn(r["hispanic_latino"]),
                _yn(r["ell_status"]),
            ]

    return _generate


async def _discover(db: AsyncSession) -> None:
    """Query roster_snapshots for distinct schools, look up display names
    from settings, and register one Exporter per school."""
    schools = [r[0] for r in (await db.execute(sa_text("""
        SELECT DISTINCT school FROM roster_snapshots
        WHERE school IS NOT NULL AND school <> ''
          AND status IN ('active', 'inactive')
          AND gender IS NOT NULL AND gender <> ''
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
        logger.warning(f"swis_students: school_names lookup failed: {e}")

    for code in schools:
        display = school_names.get(code, code)
        register(Exporter(
            id=f"swis-students-{code.lower()}",
            name=f"SWIS Student Import — {display}",
            for_app="SWIS PBIS Person Import",
            description=(
                f"All active students at {display} ({code}) whose SIS "
                f"record is in today's authoritative Clever export. SWIS "
                f"is a per-school subscription — upload this file to that "
                f"building's SWIS account. Emits SWIS-compliant genderId, "
                f"gradeId, race, isHispanic, isLanguageLearner, hasIEP."
            ),
            filename_pattern=f"swis-students-{code.lower()}-{{yyyymmdd}}.csv",
            generator=_make_generator(code),
            tags=["swis", "pbis", "students", code.lower()],
        ))


register_discovery(_discover)
