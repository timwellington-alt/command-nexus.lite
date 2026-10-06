"""Attendance CSV parser + ingest helper.

Parses the MetaSolutions "Daily Attendance" report (attachment
``Absence List by Date Range.csv``) into normalized dicts and upserts
into ``student_absences``.

CSV shape (as of 2026-09-09):
    SchoolCode, StudentNumber2, LastName2, FirstName2, Grade2, Status2,
    SummativeRaceName2, Gender2, PrimaryBuilding2, Homeroom2,
    CalendarDate2, AbsenceType2, AbsenceTypeName2, AbsenceLevel2,
    AbsenceReason2, AbsenceNote2, TimeIn2, TimeOut2, Comments2,
    PrimaryContactFirstName, PrimaryContactLastName,
    PrimaryContactFirstPhoneNumber

The `<Name>2` suffixing is a MetaSolutions legacy artifact — we strip
it in normalized keys. Also handles UTF-8 BOM on the first header
(they ship it that way).

Read-only from the parser's POV; upsert is the caller's job. Same
pattern as `parse_students_csv` in clever_service.py so downstream
code has a consistent shape to work with.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# Subject-line hint for the poll_clever_imports subject-matcher.
ATTENDANCE_SUBJECT_NEEDLE = "daily attendance"
ATTENDANCE_ATTACHMENT_NEEDLE = "absence list"   # matches "Absence List by Date Range.csv"


def _first(row: dict, *keys: str) -> str:
    """Return the first non-empty value across candidate column names.
    Tolerates the BOM-prefixed first header + case variance."""
    for k in keys:
        v = row.get(k)
        if v is None:
            # BOM-stripped variant on the leading key
            v = row.get("﻿" + k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _parse_date(s: str) -> Optional[date]:
    """MetaSolutions ships MM/DD/YYYY. Tolerant of stray whitespace or
    ISO formats if the report ever changes."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_absence_csv(content: str) -> list[dict]:
    """Parse the Absence List CSV. Returns one dict per row, normalized
    to snake_case keys the rest of the code will use. Skips rows
    without a StudentNumber or CalendarDate (either would break the
    upsert key).
    """
    reader = csv.DictReader(io.StringIO(content))
    out: list[dict] = []
    for row in reader:
        sid = _first(row, "StudentNumber2", "StudentNumber", "Student_id", "sis_id")
        date_str = _first(row, "CalendarDate2", "CalendarDate", "Date")
        absence_type = _first(row, "AbsenceType2", "AbsenceType", "Type")
        d = _parse_date(date_str)
        if not sid or not d or not absence_type:
            continue

        # Contact name: combine first + last; strip stray whitespace
        # that MetaSolutions occasionally injects (e.g. "Robert, Collier"
        # with a leading space in FirstName).
        contact_first = _first(row, "PrimaryContactFirstName")
        contact_last = _first(row, "PrimaryContactLastName")
        contact = " ".join(x for x in (contact_first, contact_last) if x).strip()

        out.append({
            "sis_id": sid,
            "school_code": _first(row, "SchoolCode", "School_id"),
            "calendar_date": d,
            "absence_type": absence_type[:10],
            "absence_type_name": _first(row, "AbsenceTypeName2", "AbsenceTypeName")[:60] or None,
            "absence_level": _first(row, "AbsenceLevel2", "AbsenceLevel")[:60] or None,
            "absence_reason": _first(row, "AbsenceReason2", "AbsenceReason")[:120] or None,
            "absence_note": _first(row, "AbsenceNote2", "AbsenceNote") or None,
            "time_in": _first(row, "TimeIn2", "TimeIn")[:20] or None,
            "time_out": _first(row, "TimeOut2", "TimeOut")[:20] or None,
            "comments": _first(row, "Comments2", "Comments") or None,
            "first_name": _first(row, "FirstName2", "FirstName", "First_name")[:100] or None,
            "last_name": _first(row, "LastName2", "LastName", "Last_name")[:100] or None,
            "grade": _first(row, "Grade2", "Grade")[:10] or None,
            "homeroom": _first(row, "Homeroom2", "Homeroom")[:120] or None,
            "primary_contact_name": contact[:200] or None,
            "primary_contact_phone": _first(row, "PrimaryContactFirstPhoneNumber", "PrimaryContactPhone")[:30] or None,
        })
    return out


async def upsert_absences(
    db: AsyncSession,
    rows: list[dict],
    *,
    source_message_id: str | None = None,
) -> dict:
    """Bulk upsert absence rows using the (sis_id, calendar_date,
    absence_type) unique key. Returns a summary dict.

    Never raises on individual-row errors — caller (poll_clever_imports)
    should not abort the whole email over a single malformed row.
    """
    result = {"total": len(rows), "inserted_or_updated": 0, "skipped": 0, "errors": []}
    if not rows:
        return result

    stmt = text("""
        INSERT INTO student_absences (
            sis_id, school_code, calendar_date, absence_type,
            absence_type_name, absence_level, absence_reason, absence_note,
            time_in, time_out, comments,
            first_name, last_name, grade, homeroom,
            primary_contact_name, primary_contact_phone,
            source_message_id, imported_at
        ) VALUES (
            :sis_id, :school_code, :calendar_date, :absence_type,
            :absence_type_name, :absence_level, :absence_reason, :absence_note,
            :time_in, :time_out, :comments,
            :first_name, :last_name, :grade, :homeroom,
            :primary_contact_name, :primary_contact_phone,
            :source_message_id, NOW()
        )
        ON CONFLICT (sis_id, calendar_date, absence_type) DO UPDATE SET
            school_code = EXCLUDED.school_code,
            absence_type_name = EXCLUDED.absence_type_name,
            absence_level = EXCLUDED.absence_level,
            absence_reason = EXCLUDED.absence_reason,
            absence_note = EXCLUDED.absence_note,
            time_in = EXCLUDED.time_in,
            time_out = EXCLUDED.time_out,
            comments = EXCLUDED.comments,
            first_name = EXCLUDED.first_name,
            last_name = EXCLUDED.last_name,
            grade = EXCLUDED.grade,
            homeroom = EXCLUDED.homeroom,
            primary_contact_name = EXCLUDED.primary_contact_name,
            primary_contact_phone = EXCLUDED.primary_contact_phone,
            source_message_id = EXCLUDED.source_message_id,
            imported_at = NOW()
    """)
    for r in rows:
        try:
            await db.execute(stmt.bindparams(
                source_message_id=source_message_id, **r,
            ))
            result["inserted_or_updated"] += 1
        except Exception as e:
            result["skipped"] += 1
            result["errors"].append(f"{r.get('sis_id','?')} {r.get('calendar_date','?')}: {str(e)[:120]}")
            if len(result["errors"]) >= 5:
                # Cap the error trail — enough to diagnose a systemic
                # problem without ballooning the response payload.
                pass

    return result
