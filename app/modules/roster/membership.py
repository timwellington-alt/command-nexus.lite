"""Student membership status — authoritative SIS-side enrollment code.

Populated from the MetaSolutions "Memberships All Yrs All Info" CSV
(subject prefix "Student Membership executed at"). Each source row is
per-membership-program; multiple rows per student but every row for
one student carries the same StudentStatus, so we dedupe on
StudentNumber.

The status code (`A`, `R`, `CTC`, etc.) maps via a Settings JSON to
a display label + `enrolled` boolean. Downstream consumers use the
`effective_enrolled` helper which layers a per-student "Attends our
classes" override on top of the code default.

See app/modules/roster/models.py for the tables (a201 migration).
"""
from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# Seed mapping — every code we've seen or Tim's operations team named.
# Editable via /settings/student-membership; this is only used when the
# settings key is unset (first boot / migration).
# Per code, three flags drive downstream automation:
#   enrolled          — student is on our services (schedule, guidance,
#                       Google account, etc.). Combined with the per-
#                       student "attends our classes" override to
#                       compute effective_enrolled.
#   auto_deprovision  — sweep should suspend/archive the Google account
#                       when a student flips to this code (defaults True
#                       only for I and D; every A/E-ish code stays False
#                       so kids at other schools keep their the district account
#                       for records-transfer purposes).
# Every consumer downstream reads these — check_liveness, guidance
# auto-queue, deprovision sweep, roster analytics.
DEFAULT_CODE_MAP: dict[str, dict] = {
    "A":    {"label": "Active Res",                        "enrolled": True,  "auto_deprovision": False},
    "N":    {"label": "Non-Resident",                      "enrolled": True,  "auto_deprovision": False},
    "E":    {"label": "Non-Res ESC Unit Student",          "enrolled": True,  "auto_deprovision": False},
    "TVA":  {"label": "Trojan Virtual Academy",            "enrolled": True,  "auto_deprovision": False},
    "TVAN": {"label": "Trojan Virtual Academy Non-Res",    "enrolled": True,  "auto_deprovision": False},
    # CTC/CTCN default to false — most CTC kids attend the Career
    # Technical Center full-day and only some take specific classes at
    # the district. Those individual cases opt in via the per-student
    # "Attends our classes" override.
    "CTC":  {"label": "Career Technical Center",           "enrolled": False, "auto_deprovision": False},
    "CTCN": {"label": "Career Technical Center Non-Res",   "enrolled": False, "auto_deprovision": False},
    "R":    {"label": "Resident, Attends Elsewhere",       "enrolled": False, "auto_deprovision": False},
    "CP":   {"label": "Court-Placed, Attends Elsewhere",   "enrolled": False, "auto_deprovision": False},
    "F":    {"label": "Foster, Attends Elsewhere",         "enrolled": False, "auto_deprovision": False},
    "IT":   {"label": "Itinerant",                         "enrolled": False, "auto_deprovision": False},
    "O":    {"label": "Other Non-Resident",                "enrolled": False, "auto_deprovision": False},
    "RJDC": {"label": "Juvenile Detention",                "enrolled": False, "auto_deprovision": False},
    "I":    {"label": "Inactive",                          "enrolled": False, "auto_deprovision": True},
    "D":    {"label": "Deleted",                           "enrolled": False, "auto_deprovision": True},
}


@dataclass
class ImportResult:
    total_rows: int = 0
    unique_students: int = 0
    upserted: int = 0
    codes_seen: dict[str, int] | None = None
    unknown_codes: list[str] | None = None


async def get_code_map(db: AsyncSession) -> dict[str, dict]:
    """Read the current code map from settings; fall back to defaults."""
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "roster", "membership_code_map")
    if not raw:
        return DEFAULT_CODE_MAP
    try:
        m = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(m, dict) and m:
            return m
    except (json.JSONDecodeError, TypeError):
        logger.warning("membership_code_map setting is not valid JSON; using defaults")
    return DEFAULT_CODE_MAP


async def effective_enrolled(db: AsyncSession, sis_id: str) -> bool | None:
    """Is this student enrolled in the district services per authoritative SIS?

    Returns:
        True  — code says enrolled OR per-student override says attends
        False — code says not enrolled AND no override
        None  — no membership row for this student (unknown; caller
                should treat as insufficient info, not "not enrolled")
    """
    row = (await db.execute(text(
        "SELECT code FROM student_membership_status WHERE sis_id = :s"
    ).bindparams(s=sis_id))).mappings().first()
    if not row:
        return None
    override = (await db.execute(text(
        "SELECT 1 FROM student_attends_our_classes WHERE sis_id = :s"
    ).bindparams(s=sis_id))).scalar_one_or_none()
    if override:
        return True
    code_map = await get_code_map(db)
    entry = code_map.get(row["code"], {})
    return bool(entry.get("enrolled", False))


async def bulk_effective_enrolled(db: AsyncSession, sis_ids: list[str]) -> dict[str, bool | None]:
    """Batch version — one query for the whole set."""
    if not sis_ids:
        return {}
    rows = (await db.execute(text("""
        SELECT sis_id, code FROM student_membership_status
        WHERE sis_id = ANY(CAST(:s AS text[]))
    """).bindparams(s=sis_ids))).mappings().all()
    code_by_sid = {r["sis_id"]: r["code"] for r in rows}

    overrides = (await db.execute(text("""
        SELECT sis_id FROM student_attends_our_classes
        WHERE sis_id = ANY(CAST(:s AS text[]))
    """).bindparams(s=sis_ids))).scalars().all()
    override_set = set(overrides)

    code_map = await get_code_map(db)
    out: dict[str, bool | None] = {}
    for sid in sis_ids:
        code = code_by_sid.get(sid)
        if code is None:
            out[sid] = None
            continue
        if sid in override_set:
            out[sid] = True
            continue
        out[sid] = bool(code_map.get(code, {}).get("enrolled", False))
    return out


async def import_csv(db: AsyncSession, csv_bytes: bytes,
                     source_subject: str | None = None) -> ImportResult:
    """Parse a "Memberships All Yrs All Info" CSV and upsert one row
    per unique StudentNumber into student_membership_status.

    Multiple source rows per student are expected (one per membership
    program); we take the first row per student. Verified against a
    real 10,911-row file that no student has conflicting StudentStatus
    across their rows.

    Ignores program-detail columns (MembershipName, StaffName, etc.)
    's direction — the primary signal is StudentStatus.
    """
    from collections import Counter

    result = ImportResult(codes_seen=Counter(), unknown_codes=[])
    text_content = csv_bytes.decode("utf-8-sig", errors="replace")
    rdr = csv.DictReader(io.StringIO(text_content))
    seen: dict[str, dict] = {}
    for row in rdr:
        result.total_rows += 1
        sid = (row.get("StudentNumber") or "").strip()
        code = (row.get("StudentStatus") or "").strip()
        if not sid or not code:
            continue
        # First-wins dedup. Verified safe (no per-student status conflicts).
        if sid in seen:
            continue
        seen[sid] = {
            "sis_id": sid,
            "code": code,
            "district_withdrawal_date": (row.get("DistrictWithdrawalDate") or "").strip() or None,
            "district_withdrawal_reason": (row.get("DistrictWithdrawalReason") or "").strip() or None,
        }
    result.unique_students = len(seen)
    for s in seen.values():
        result.codes_seen[s["code"]] += 1

    if not seen:
        return result

    # Validate codes against the current map — flag unknowns for the
    # UI to prompt an operator to add labels.
    code_map = await get_code_map(db)
    result.unknown_codes = sorted(
        {c for c in result.codes_seen if c not in code_map}
    )

    # Bulk upsert. Truncate + insert would be faster but ON CONFLICT
    # preserves imported_at ordering better and lets us keep a per-row
    # source_subject for provenance.
    now = datetime.now(timezone.utc)
    rows_payload = [
        {**s, "imported_at": now.isoformat(), "source_subject": source_subject}
        for s in seen.values()
    ]
    await db.execute(text("""
        INSERT INTO student_membership_status
            (sis_id, code, district_withdrawal_date, district_withdrawal_reason,
             imported_at, source_subject)
        SELECT sis_id, code, district_withdrawal_date, district_withdrawal_reason,
               CAST(imported_at AS timestamptz), source_subject
        FROM jsonb_to_recordset(CAST(:rows AS jsonb))
          AS x(sis_id text, code text,
               district_withdrawal_date text, district_withdrawal_reason text,
               imported_at text, source_subject text)
        ON CONFLICT (sis_id) DO UPDATE SET
            code                       = EXCLUDED.code,
            district_withdrawal_date   = EXCLUDED.district_withdrawal_date,
            district_withdrawal_reason = EXCLUDED.district_withdrawal_reason,
            imported_at                = EXCLUDED.imported_at,
            source_subject             = EXCLUDED.source_subject
    """), {"rows": json.dumps(rows_payload)})
    result.upserted = len(rows_payload)
    return result
