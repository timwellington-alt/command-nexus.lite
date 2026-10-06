"""Clever custom-sections pipeline.

Teachers hand-populate a Google Sheet, one tab per internal building code
(PES/EPE/PHS). Nexus:

  1. Reads every non-empty data row across all tabs
  2. Validates: joins student SID (or name→SID fallback) to
     roster_snapshots, teacher email to staff_directory
  3. Writes a Match Status column back to the sheet so teachers see
     validation results in place
  4. Builds sections.csv + enrollments.csv per Clever's SIS-import
     format (see Project Specs/Clever_custom/)
  5. Dry-run: writes CSVs to docs/clever_custom_out/{date}/
     Live:    SFTP-uploads to Clever's customsections/ folder

Section_id is deterministic across syncs (custom-{sis}-{teacher-slug}-
{section-slug}) so re-uploads UPDATE existing Clever sections rather
than tearing them down and re-creating (which would drop the
enrollments too).

All external writes (sheet write-back + SFTP puts) are audit-logged
with a SHA256 of the payload so we can prove what left Nexus at
what time.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


SHEET_COLS = [
    "section_name", "teacher_email", "student_last", "student_first",
    "student_sid", "match_status", "notes",
]


# ── Data classes ───────────────────────────────────────────────────


@dataclass
class SheetRow:
    tab: str            # teacher last name (e.g. 'Taylor')
    row_index: int      # 1-based row number in the sheet (for write-back)
    section_name: str
    teacher_email: str
    student_last: str
    student_first: str
    student_sid: str    # raw input, may be blank
    notes: str
    internal_building: str = ""  # from metadata "Building:" (PES/EPE/PHS)
    grade: str = ""              # from metadata "Grade:" — teacher OVERRIDE (blank = infer)
    period: str = ""             # from metadata "Period:" (optional)
    # Populated by validate()
    resolved_sid: str | None = None
    student_grade: str = ""      # this student's grade from roster (for section-mode inference)
    match_status: str = ""
    building_sis: str | None = None  # SIS code for CSV output


# District-wide constant  (2026-09-10) — every custom section is
# an intervention or online-learning support. If this ever needs to
# vary by section, promote it to a per-tab metadata cell.
DEFAULT_SUBJECT = "Interventions/Online Learning"


def pull_teacher_id_map(*, host: str, port: int, username: str, password: str) -> dict[str, str]:
    """Pull Clever's teachers.csv from the SFTP root and return
    {lowercase_email: Teacher_id}.

    Clever's `Teacher_id` column stores district-assigned short codes
    (e.g. 'CLEM', 'RATL', sometimes reflecting a prior surname like
    Taylor→'WARR'). Custom-section rows MUST use those exact ids or
    Clever rejects with 'Teacher_id: Missing required field' — the
    email address alone is not a valid join key.

    Called once per sync run — small file (~13 KB / 150 rows), no
    caching needed."""
    import paramiko
    import csv as _csv
    import io as _io
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.rstrip("/")
    t = paramiko.Transport((host, port))
    try:
        t.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(t)
        try:
            with sftp.open("teachers.csv", "r") as fh:
                data = fh.read().decode("utf-8")
        finally:
            sftp.close()
    finally:
        t.close()
    mapping: dict[str, str] = {}
    rdr = _csv.DictReader(_io.StringIO(data))
    for row in rdr:
        email = ""
        tid = ""
        for k, v in row.items():
            k_low = (k or "").lower().lstrip("﻿")
            if k_low == "teacher_email":
                email = (v or "").strip().lower()
            elif k_low == "teacher_id":
                tid = (v or "").strip()
        if email and tid:
            mapping[email] = tid
    return mapping


@dataclass
class SyncResult:
    dry_run: bool
    tabs_read: dict[str, int] = field(default_factory=dict)  # tab → row count
    sections_built: int = 0
    enrollments_built: int = 0
    matched: int = 0
    ambiguous: int = 0
    missing_sid: int = 0
    invalid_teacher: int = 0
    teacher_map_size: int = 0
    custom_teachers_built: int = 0
    custom_students_built: int = 0
    sections_skipped_no_teacher_id: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    upload_ok: bool = False
    upload_details: dict[str, str] = field(default_factory=dict)  # filename → sha256
    errors: list[str] = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────


def slug(s: str) -> str:
    """Lowercase, non-alphanumeric → `-`, collapse repeats, strip."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower())
    return re.sub(r"-+", "-", s).strip("-")


def section_id(sis_code: str, teacher_email: str, section_name: str) -> str:
    """Deterministic id — stable across sheet edits so Clever keeps state.

    Uses only the email LOCAL part (before @) to keep the id short —
    the whole email domain would double the length for no gain in
    uniqueness within the district."""
    local = teacher_email.split("@", 1)[0] if "@" in teacher_email else teacher_email
    return f"custom-{sis_code}-{slug(local)}-{slug(section_name)}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Google Sheets access ──────────────────────────────────────────


def _sheets_client(readonly: bool = False):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    cred_file = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "/run/secrets/google_service_account.json",
    )
    scopes = ["https://www.googleapis.com/auth/spreadsheets" + (".readonly" if readonly else "")]
    creds = service_account.Credentials.from_service_account_file(cred_file, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(sheet_id: str) -> list[SheetRow]:
    """Pull every student row across every teacher tab.

    Layout (tab-per-teacher):

        Rows 1..N: metadata block — one 'Label: value' per row
                   (Building:, Teacher:, Grade:, Period:, …).
                   Terminated by the first blank row.
        Next non-blank row: column header (Last | First | SID | …)
        Rest: student rows.

    Detection: any tab whose A1 cell starts with 'Building:' is a
    class tab. TEMPLATE, INSTRUCTIONS, and anything else are silently
    skipped so operators can add helper tabs without polluting sync.

    Flexible metadata: keys are case-insensitive; unknown keys are
    stored in a dict but ignored by the pipeline (future-proofs the
    schema — add a 'Subject:' or 'Course number:' row later without
    breaking existing tabs).

    Skip rules per tab (silent, no error):
      * Building: cell blank or starts with '(fill in'
      * Teacher: cell blank / '(fill in'
      * Grade: cell blank / '(fill in'   ← required by Clever

    Tab name (teacher last name) → section_name = '{Tab}-intervention'
    and part of the deterministic Section_id.
    """
    svc = _sheets_client(readonly=True)
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    rows: list[SheetRow] = []
    for s in meta.get("sheets", []):
        title = s["properties"]["title"]
        r = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{title}'!A1:E2000",
        ).execute()
        vals = r.get("values", [])
        if not vals:
            continue

        # Detection gate — A1 must start with "Building:"
        first_cell = (vals[0][0] if vals[0] else "").strip()
        if not first_cell.startswith("Building:"):
            continue

        # Parse metadata block — dict of lowercased key → value.
        # Terminate on first blank row (row where col A + col B both
        # empty). Header row starts after that blank.
        metadata: dict[str, str] = {}
        header_row_idx = None
        for i, row in enumerate(vals):
            a = (row[0] or "").strip() if row else ""
            b = (row[1] or "").strip() if len(row) > 1 else ""
            if not a and not b:
                # Blank row — end of metadata block. Header is the
                # NEXT non-blank row.
                for j in range(i + 1, len(vals)):
                    if vals[j] and any((c or "").strip() for c in vals[j]):
                        header_row_idx = j
                        break
                break
            if a.endswith(":"):
                key = a.rstrip(":").strip().lower()
                metadata[key] = b
        if header_row_idx is None:
            continue

        internal_building = metadata.get("building", "")
        teacher_email = metadata.get("teacher", "").lower()
        grade = metadata.get("grade", "")
        period = metadata.get("period", "")

        # Skip if Building or Teacher unfilled. Grade is OPTIONAL in
        # the sheet — if left as "(fill in…)" or blank, we infer from
        # the students' actual grades during CSV build (section mode).
        def _incomplete(v: str) -> bool:
            return not v or v.startswith("(fill in")
        if _incomplete(internal_building) or _incomplete(teacher_email):
            continue
        if _incomplete(grade):
            grade = ""  # blank → infer during build_sections_csv

        # Student rows start right after the header row.
        # Columns: A=Last B=First C=SID D=MatchStatus E=Notes
        for idx_zero, row in enumerate(vals[header_row_idx + 1:], start=header_row_idx + 2):
            row = row + [""] * (5 - len(row))
            last = (row[0] or "").strip()
            first = (row[1] or "").strip()
            sid = (row[2] or "").strip()
            notes = (row[4] or "").strip() if len(row) > 4 else ""
            if not any([last, first, sid, notes]):
                continue
            rows.append(SheetRow(
                tab=title,
                row_index=idx_zero,
                section_name=f"{title}-intervention",
                teacher_email=teacher_email,
                student_last=last,
                student_first=first,
                student_sid=sid,
                notes=notes,
                internal_building=internal_building,
                grade=grade,
                period=period,
            ))
    return rows


def write_match_status(sheet_id: str, rows: list[SheetRow]) -> None:
    """Batch-write column D (Match Status) for every row we touched.

    Uses batchUpdate values.update with a single per-tab range so we
    don't hit the per-cell API rate limit. Never touches other cols.

    Column D in the current layout:
      A=Last | B=First | C=SID | D=MatchStatus | E=Notes
    """
    if not rows:
        return
    svc = _sheets_client(readonly=False)
    data = []
    for r in rows:
        data.append({
            "range": f"'{r.tab}'!D{r.row_index}",
            "values": [[r.match_status]],
        })
    if not data:
        return
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()


_DASHBOARD_TAB = "Dashboard"


def write_dashboard(sheet_id: str, rows: list[SheetRow]) -> None:
    """Write a Dashboard tab showing rows that need teacher attention.

    Two sections:
      A) Mismatched rows — Match Status starts with ✗. Teacher typed
         a SID / name that couldn't be resolved; sync is skipping them.
      B) Custom students with no email — lifted to customstudents.csv
         but no email extracted from Notes, so Clever can't match them
         to a Google account. Fixable by pasting an email in Notes.

    Creates the tab on first run, clears + rewrites on every sync.
    Placed at the front so teachers see it when they open the sheet."""
    from datetime import datetime, timezone

    mismatched = [
        r for r in rows
        if (r.match_status or "").startswith("✗")
    ]
    no_email_custom = [
        r for r in rows
        if (r.match_status or "").endswith("(custom student)")
        and not parse_email_from_notes(r.notes)
    ]

    svc = _sheets_client(readonly=False)
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    dash_sheet_id = None
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == _DASHBOARD_TAB:
            dash_sheet_id = s["properties"]["sheetId"]
            break

    # First-ever run — create the tab pinned to index 0 so it's first.
    if dash_sheet_id is None:
        add_resp = svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": _DASHBOARD_TAB, "index": 0,
                "gridProperties": {"frozenRowCount": 1},
            }}}]},
        ).execute()
        dash_sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Build the value grid.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    values: list[list[str]] = [
        [f"Sync dashboard — last updated {ts}", "", "", "", "", ""],
        [""],
        [
            f"Mismatched students ({len(mismatched)})",
            "", "", "", "", "",
        ],
        ["Tab", "Teacher", "Last", "First", "SID", "Match Status"],
    ]
    if not mismatched:
        values.append(["(none — all typed SIDs / names resolved)", "", "", "", "", ""])
    for r in sorted(mismatched, key=lambda x: (x.tab, x.row_index)):
        values.append([
            r.tab, r.teacher_email, r.student_last, r.student_first,
            r.student_sid, r.match_status,
        ])

    values.append([""])
    values.append([
        f"Custom students missing email ({len(no_email_custom)})",
        "", "", "", "", "",
    ])
    values.append([
        "Tab", "Teacher", "Last", "First", "SID",
        "Fix — paste email in Notes column",
    ])
    if not no_email_custom:
        values.append(["(none — every custom student has an email)", "", "", "", "", ""])
    for r in sorted(no_email_custom, key=lambda x: (x.tab, x.row_index)):
        values.append([
            r.tab, r.teacher_email, r.student_last, r.student_first,
            r.student_sid, "",
        ])

    # Clear + write in one shot. Wide range so old rows from a prior
    # run get erased even if the new dashboard is shorter.
    svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"'{_DASHBOARD_TAB}'!A1:F5000",
        body={},
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{_DASHBOARD_TAB}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    # Format the header + section titles — bold, slightly larger,
    # background fill for the two section title rows. Two requests
    # per section so a re-run doesn't stack formats.
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [
            {"repeatCell": {
                "range": {"sheetId": dash_sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": 6},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True, "fontSize": 12},
                }},
                "fields": "userEnteredFormat.textFormat",
            }},
            {"repeatCell": {
                "range": {"sheetId": dash_sheet_id, "startRowIndex": 2, "endRowIndex": 3,
                          "startColumnIndex": 0, "endColumnIndex": 6},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.95, "green": 0.85, "blue": 0.85},
                    "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }},
            {"repeatCell": {
                "range": {"sheetId": dash_sheet_id,
                          "startRowIndex": 4 + max(len(mismatched), 1) + 1,
                          "endRowIndex":   4 + max(len(mismatched), 1) + 2,
                          "startColumnIndex": 0, "endColumnIndex": 6},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.80},
                    "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }},
        ]},
    ).execute()


# ── Validation ─────────────────────────────────────────────────────


async def _sis_code_for_internal(db: AsyncSession, internal: str) -> str | None:
    """Map internal building code (PES/PHS/EPE) → SIS code (SIS_A/SIS_B/SIS_C).

    Reverse of resolve_building_code — we need the raw SIS code because
    that's what Clever expects in School_id (and what roster_snapshots
    stores in .school)."""
    from app.modules.settings.buildings import get_building_maps
    bmap = await get_building_maps(db)
    sis_to_internal = bmap.get("sis_to_internal", {})
    for sis, mapped in sis_to_internal.items():
        if mapped == internal:
            return sis
    return None


async def validate_rows(db: AsyncSession, rows: list[SheetRow]) -> None:
    """Populate resolved_sid + match_status on each row in-place.

    Building translation: row.internal_building comes from the tab's
    B1 metadata cell (PES/EPE/PHS). We convert it to the SIS code
    (SIS_A/SIS_B/SIS_C) once per unique value and cache."""
    internals = {r.internal_building for r in rows if r.internal_building}
    sis_map: dict[str, str | None] = {}
    for internal in internals:
        sis_map[internal] = await _sis_code_for_internal(db, internal)

    # Cache staff_directory emails (lowercase) for teacher check
    staff_emails = {
        r[0].lower() for r in (await db.execute(text(
            "SELECT email FROM staff_directory WHERE email IS NOT NULL AND email <> ''"
        ))).all() if r[0]
    }

    for r in rows:
        r.building_sis = sis_map.get(r.internal_building)
        if r.building_sis is None:
            r.match_status = (
                f"✗ Building cell says {r.internal_building!r} — "
                f"expected PES / EPE / PHS"
            )
            continue

        # Teacher email must exist in staff_directory
        if r.teacher_email and r.teacher_email not in staff_emails:
            r.match_status = f"⚠ teacher email not in staff directory — will still send"

        # Student resolution
        if r.student_sid:
            # SID provided — verify it exists at this school.
            # Accept status='active' OR a membership code the operator
            # has marked enrolled OR a per-student attends-our-classes
            # override. Kids on A/E-ish codes (F, R, CTC, etc.) drop out
            # of our daily active-roster feed but still exist in Clever
            # and legitimately need custom-section enrollments — the
            # membership tables (a201) are what we trust here, not the
            # derived status boolean.
            row = (await db.execute(text("""
                SELECT rs.sis_id, rs.first_name, rs.last_name, rs.school, rs.grade,
                       rs.status
                FROM roster_snapshots rs
                LEFT JOIN student_membership_status sms ON sms.sis_id = rs.sis_id
                LEFT JOIN student_attends_our_classes ao ON ao.sis_id = rs.sis_id
                WHERE rs.sis_id = :sid
                  AND (
                    rs.status = 'active'
                    OR ao.sis_id IS NOT NULL
                    OR sms.code IS NOT NULL
                  )
                LIMIT 1
            """).bindparams(sid=r.student_sid))).mappings().first()
            if not row:
                r.match_status = "✗ SID not found on active roster"
                continue
            if row["school"] != r.building_sis:
                r.match_status = (
                    f"⚠ SID {r.student_sid} belongs to {row['school']}, "
                    f"not {r.building_sis} — check tab"
                )
                r.resolved_sid = r.student_sid
                r.student_grade = (row["grade"] or "").strip()
                continue
            # Optional name-mismatch warn
            roster_name = f"{row['first_name']} {row['last_name']}".lower()
            sheet_name = f"{r.student_first} {r.student_last}".lower()
            r.resolved_sid = r.student_sid
            r.student_grade = (row["grade"] or "").strip()
            if roster_name != sheet_name:
                r.match_status = (
                    f"⚠ SID/name mismatch — roster says "
                    f"{row['first_name']} {row['last_name']}"
                )
                continue
            r.match_status = f"✓ {r.student_sid}"
        else:
            # No SID — name-match against roster in this building
            matches = (await db.execute(text("""
                SELECT sis_id, first_name, last_name, grade
                FROM roster_snapshots
                WHERE school = :sc AND status = 'active'
                  AND LOWER(last_name) = LOWER(:ln)
                  AND LOWER(first_name) = LOWER(:fname)
                LIMIT 5
            """).bindparams(sc=r.building_sis, ln=r.student_last, fname=r.student_first))).all()
            if not matches:
                r.match_status = "✗ no student found — check spelling or add SID"
            elif len(matches) > 1:
                r.match_status = f"⚠ ambiguous — {len(matches)} matches, add SID"
            else:
                r.resolved_sid = matches[0][0]
                r.student_grade = (matches[0][3] or "").strip()
                r.match_status = f"✓ {matches[0][0]}"


# ── CSV builders ───────────────────────────────────────────────────


SECTIONS_HEADER = [
    "School_id", "Section_id", "Teacher_id", "Teacher_2_id", "Teacher_3_id",
    "Teacher_4_id", "Name", "Section_number", "Grade", "Course_name",
    "Course_number", "Course_description", "Period", "Subject", "Term_name",
    "Term_start", "Term_end",
]
ENROLLMENTS_HEADER = ["School_id", "Section_id", "Student_id"]
# Full teachers.csv header from Project Specs/Clever_custom/. Custom
# teachers only need the required fields (School_id, Teacher_id,
# Last_name, First_name, Teacher_email); everything else stays blank.
CUSTOMTEACHERS_HEADER = [
    "School_id", "Teacher_id", "Teacher_number", "State_teacher_id",
    "Last_name", "Middle_name", "First_name", "Teacher_email",
    "Title", "Username", "Password",
]
# Minimal customstudents header — School_id, Student_id, Student_number,
# Last_name, First_name, Grade are the fields Clever needs for a
# custom student record. Student_email is optional per Clever's SIS
# spec but REQUIRED for their identity resolution to actually match
# the custom student to their auto-provisioned Google account (Tim
# 2026-09-22). Teachers put the email in the Notes column when
# they know it; parse_email_from_notes lifts it into this field.
CUSTOMSTUDENTS_HEADER = [
    "School_id", "Student_id", "Student_number",
    "Last_name", "First_name", "Grade", "Student_email",
]

# Loose email regex — grabs the first email-shaped token from a free-text
# Notes cell. Case-insensitive, unicode-friendly enough for a district
# email format (firstname.lastname@ / lastnameXX@). Does NOT try to
# validate the domain — a teacher pasting "kaiser.detty@yourdistrict.org
# (temporary)" gets the address extracted cleanly.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def parse_email_from_notes(notes: str) -> str:
    """Return the first email address found in a Notes cell, lowercased.
    Empty string if none found. Trims trailing punctuation so
    "email: foo@bar.net," yields "foo@bar.net"."""
    if not notes:
        return ""
    m = _EMAIL_RE.search(notes)
    if not m:
        return ""
    return m.group(0).lower().strip(".,;: ")


async def resolve_missing_teachers(
    db: AsyncSession,
    sheet_rows: list[SheetRow],
    teacher_id_map: dict[str, str],
) -> tuple[dict[str, str], list[dict]]:
    """For any teacher email in sheet_rows not already in teacher_id_map,
    mint a deterministic custom Teacher_id from staff_directory data.

    Returns (augmented_map, custom_teachers_to_ship). The map gets the
    new Teacher_ids added in-place so build_sections_csv can reference
    them. custom_teachers_to_ship is the row list for
    build_customteachers_csv — one dict per teacher with school/name/
    email fields.

    A missing teacher who ALSO isn't in staff_directory stays missing;
    their sections still get skipped and reported.
    """
    from app.modules.settings.buildings import get_building_maps

    needed = {r.teacher_email for r in sheet_rows if r.teacher_email and r.teacher_email not in teacher_id_map}
    if not needed:
        return teacher_id_map, []

    bmap = await get_building_maps(db)
    sis_to_internal = bmap.get("sis_to_internal", {})
    internal_to_sis = {v: k for k, v in sis_to_internal.items()}

    # Pull one row per missing email
    rows = (await db.execute(text("""
        SELECT LOWER(email) AS email_lc, first_name, last_name, building
        FROM staff_directory
        WHERE LOWER(email) = ANY(:emails)
    """).bindparams(emails=sorted(needed)))).mappings().all()

    custom_teachers: list[dict] = []
    for r in rows:
        email = r["email_lc"]
        internal_bldg = (r["building"] or "").strip()
        sis_bldg = internal_to_sis.get(internal_bldg)
        if not sis_bldg:
            # Can't ship a teacher without a valid school. Leave off
            # the map so their sections still get flagged.
            continue
        local = email.split("@", 1)[0] if "@" in email else email
        tid = f"CUSTOM-{slug(local)}".upper()
        teacher_id_map[email] = tid
        custom_teachers.append({
            "school_id": sis_bldg,
            "teacher_id": tid,
            "first_name": (r["first_name"] or "").strip(),
            "last_name":  (r["last_name"]  or "").strip(),
            "email":      email,
        })
    return teacher_id_map, custom_teachers


def resolve_missing_students(sheet_rows: list[SheetRow]) -> list[dict]:
    """For any sheet row where the teacher typed a raw SID but the SID
    doesn't exist in Nexus AND isn't in Clever's SIS feed, mint a
    customstudents record so Clever will accept the enrollment.

    Two other rejection modes bypass this fallback because they aren't
    trustable inputs:
      - no SID typed + no name match → could be a misspelling, would
        create a phantom student record; teacher must add a real SID
      - name-matched but ambiguous → teacher must disambiguate

    Sets `r.resolved_sid = r.student_sid` on each row we lifted so
    build_enrollments_csv picks it up. Returns the list of customstudent
    dicts ready for build_customstudents_csv.
    """
    lifted: list[dict] = []
    seen: set[str] = set()
    for r in sheet_rows:
        # Only rows that have a raw typed SID + failed SID-lookup qualify.
        if r.resolved_sid or not r.student_sid:
            continue
        if r.match_status and not r.match_status.startswith("✗ SID not found"):
            continue
        if not r.building_sis:
            continue
        # Set the resolved_sid so downstream enrollment CSV includes it.
        r.resolved_sid = r.student_sid
        r.match_status = f"✓ {r.student_sid} (custom student)"
        key = (r.building_sis, r.student_sid)
        if key in seen:
            continue
        seen.add(key)
        # Grade — prefer the tab-level Grade; fall back to blank (Clever
        # will still ingest and warn). student_grade isn't populated on
        # these rows because we never found a roster hit to read it from.
        grade = (r.grade or "").strip()
        # Email — pulled from the Notes column if the teacher pasted one
        # there. Required by Clever to resolve the custom student to
        # their Google account; without it enrollments land but the
        # student can't sign in through the Clever launcher.
        email = parse_email_from_notes(r.notes)
        lifted.append({
            "school_id":      r.building_sis,
            "student_id":     r.student_sid,
            "student_number": r.student_sid,
            "last_name":      (r.student_last or "").strip(),
            "first_name":     (r.student_first or "").strip(),
            "grade":          grade,
            "email":          email,
        })
    return lifted


def build_customstudents_csv(students: list[dict]) -> tuple[str, int]:
    """One row per custom student. Same always-upload contract as
    customteachers: if empty, ship a header-only file so Clever
    de-registers any obsolete rows from a prior run."""
    seen: set[str] = set()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CUSTOMSTUDENTS_HEADER)
    count = 0
    for s in students:
        sid = s["student_id"]
        if sid in seen:
            continue
        seen.add(sid)
        w.writerow([
            s["school_id"],           # School_id
            sid,                      # Student_id
            s["student_number"],      # Student_number
            s["last_name"],           # Last_name
            s["first_name"],          # First_name
            s["grade"],               # Grade
            s.get("email", ""),       # Student_email (blank if teacher didn't
                                      # supply one in Notes — row still ships;
                                      # Clever will accept without matching)
        ])
        count += 1
    return buf.getvalue(), count


def build_customteachers_csv(teachers: list[dict]) -> tuple[str, int]:
    """One row per custom teacher. Populates only required Clever
    fields (School_id, Teacher_id, Last_name, First_name, Teacher_email).
    """
    seen: set[str] = set()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CUSTOMTEACHERS_HEADER)
    count = 0
    for t in teachers:
        tid = t["teacher_id"]
        if tid in seen:
            continue
        seen.add(tid)
        w.writerow([
            t["school_id"],       # School_id
            tid,                  # Teacher_id
            "",                   # Teacher_number
            "",                   # State_teacher_id
            t["last_name"],       # Last_name
            "",                   # Middle_name
            t["first_name"],      # First_name
            t["email"],           # Teacher_email
            "",                   # Title
            "",                   # Username
            "",                   # Password
        ])
        count += 1
    return buf.getvalue(), count


def _mode_grade(grades: list[str]) -> str:
    """Most common grade in a section — used when the teacher didn't
    supply an explicit Grade override in the sheet metadata. Ties
    broken by preferring the numerically SMALLEST grade (KG < PS <
    1 < 2 … < 12), which is conservative for intervention grouping.
    """
    from collections import Counter
    grades = [g for g in grades if g]
    if not grades:
        return ""
    counts = Counter(grades)
    max_n = max(counts.values())
    tied = [g for g, n in counts.items() if n == max_n]
    if len(tied) == 1:
        return tied[0]
    # Tiebreak: sort by (KG=0, PS=-1 as lowest, then numeric)
    def _rank(g: str) -> tuple[int, str]:
        if g == "PS": return (-1, g)
        if g == "KG": return (0, g)
        try:
            return (int(g), g)
        except ValueError:
            return (999, g)
    return sorted(tied, key=_rank)[0]


def build_sections_csv(
    rows: list[SheetRow],
    teacher_id_map: dict[str, str] | None = None,
) -> tuple[str, int, list[str]]:
    """One row per unique (building, teacher, section).

    Populates Clever's required fields:
      School_id  ← SIS building code
      Section_id ← deterministic slug
      Teacher_id ← teacher_id_map[teacher_email] — Clever's district code
      Name       ← "{TabName}-intervention"
      Grade      ← teacher's sheet override if provided; otherwise
                    inferred from the mode of enrolled students' grades
      Subject    ← DEFAULT_SUBJECT ("Interventions/Online Learning")

    Course_name mirrors Name so Clever's course-name filter matches
    the section list. All other columns left blank.

    Returns (csv_text, section_count, skipped_teacher_emails).
    Sections whose teacher_email isn't in teacher_id_map are DROPPED
    (Clever would reject with 'Teacher_id: Missing required field')
    and their emails are collected into skipped_teacher_emails for
    operator visibility.
    """
    teacher_id_map = teacher_id_map or {}
    # Group rows by section key so we can infer Grade from all members
    by_section: dict[tuple[str, str, str], list[SheetRow]] = {}
    for r in rows:
        if not r.building_sis or not r.section_name or not r.teacher_email:
            continue
        key = (r.building_sis, r.teacher_email, r.section_name)
        by_section.setdefault(key, []).append(r)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(SECTIONS_HEADER)
    count = 0
    skipped: list[str] = []
    for (bsis, email, section_name), section_rows in by_section.items():
        clever_tid = teacher_id_map.get(email)
        if not clever_tid:
            # Silently skip — reported to caller for surfacing in
            # audit + status page. Also stamp every row of this
            # section with a match_status so the sheet shows why.
            for r in section_rows:
                if not r.match_status.startswith("✗"):
                    r.match_status = (
                        f"⚠ teacher not in Clever teachers.csv "
                        f"— section skipped until SIS syncs {email}"
                    )
            skipped.append(email)
            continue
        first = section_rows[0]
        grade = (first.grade or "").strip()
        if not grade:
            grade = _mode_grade([r.student_grade for r in section_rows])
        sid = section_id(bsis, email, section_name)
        # Display name is intentionally NOT the tab-derived section_name.
        # Clever's UI appends the teacher's last name to the display,
        # so "Vetter-intervention" becomes "Vetter-intervention - Vetter"
        # (doubled). Use a generic display label; the Section_id still
        # slugs off the tab so re-uploads update the existing sections
        # instead of creating new ones.
        display_name = "Intervention"
        w.writerow([
            bsis, sid, clever_tid,                 # Teacher_id = Clever's short code
            "", "", "",                            # Teacher_{2..4}_id blank
            display_name,                          # Name
            "",                                    # Section_number
            grade,
            display_name,                          # Course_name (mirror)
            "",                                    # Course_number
            "",                                    # Course_description
            first.period,
            DEFAULT_SUBJECT,
            "", "", "",                            # Term_name / start / end
        ])
        count += 1
    return buf.getvalue(), count, skipped


def build_enrollments_csv(
    rows: list[SheetRow],
    teacher_id_map: dict[str, str] | None = None,
) -> tuple[str, int]:
    """One row per resolved (section, student). Skips rows without a SID.

    Also skips enrollments whose teacher_email isn't in teacher_id_map
    — those sections don't ship, so their enrollments would be orphans.
    """
    teacher_id_map = teacher_id_map or {}
    seen: set[tuple[str, str, str]] = set()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ENROLLMENTS_HEADER)
    count = 0
    for r in rows:
        if not r.building_sis or not r.resolved_sid or not r.section_name:
            continue
        if r.teacher_email not in teacher_id_map:
            continue
        sid = section_id(r.building_sis, r.teacher_email, r.section_name)
        key = (r.building_sis, sid, r.resolved_sid)
        if key in seen:
            continue
        seen.add(key)
        w.writerow([r.building_sis, sid, r.resolved_sid])
        count += 1
    return buf.getvalue(), count


# ── Delivery ───────────────────────────────────────────────────────


def write_dry_run(
    sections_csv: str,
    enrollments_csv: str,
    customteachers_csv: str | None = None,
    customstudents_csv: str | None = None,
) -> list[str]:
    """Drop CSVs to docs/clever_custom_out/{date}/. Returns absolute paths."""
    out_dir = Path("/app/docs/clever_custom_out") / date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    files = [("sections.csv", sections_csv), ("enrollments.csv", enrollments_csv)]
    if customteachers_csv:
        files.append(("customteachers-teachers.csv", customteachers_csv))
    if customstudents_csv:
        files.append(("customstudents-students.csv", customstudents_csv))
    for name, content in files:
        p = out_dir / name
        p.write_text(content, encoding="utf-8")
        paths.append(str(p))
    return paths


# SIS_C SAFETY RULE — Nexus must NEVER write to the Clever SFTP root.
# The root holds the SIS's authoritative feed (teachers.csv,
# students.csv, sections.csv, etc.); writing there could clobber the
# whole district's Clever integration. Every SFTP write is gated on
# remote_path matching this allowlist. If Clever ever documents a
# new custom-import folder, add it here explicitly.
ALLOWED_REMOTE_PATHS = frozenset({
    "customsections",
    "customstudents",
    # customteachers/ — fallback for teachers missing from the SIS-fed
    # teachers.csv (Cox/Mayhew/Michael Duncan case, 2026-09-11). Clever
    # doesn't ship this folder by default; upload_sftp() auto-creates
    # it on first push. Data source is staff_directory.
    "customteachers",
})


def _validate_remote_path(remote_path: str) -> str:
    """Normalize + validate the SFTP remote path. Refuses root, refuses
    empty, refuses anything not in ALLOWED_REMOTE_PATHS. Returns the
    canonical form on success or raises RuntimeError."""
    p = (remote_path or "").strip().strip("/")
    if not p or p in {".", "..", ""} or "/" in p:
        raise RuntimeError(
            f"SFTP remote_path {remote_path!r} is unsafe. Must be a bare "
            f"subfolder name (one of: {sorted(ALLOWED_REMOTE_PATHS)}). "
            f"Writing to the Clever SFTP root would clobber the SIS's "
            f"authoritative feed."
        )
    if p not in ALLOWED_REMOTE_PATHS:
        raise RuntimeError(
            f"SFTP remote_path {p!r} is not in the allowlist "
            f"{sorted(ALLOWED_REMOTE_PATHS)}. Add it to "
            f"ALLOWED_REMOTE_PATHS in clever_custom_sections.py if "
            f"Clever has documented it as a valid custom-import target."
        )
    return p


def upload_sftp(
    *, host: str, port: int, username: str, password: str,
    remote_path: str, files: dict[str, str],
) -> dict[str, str]:
    """Put each file under remote_path/. Returns filename → sha256 map.

    Backs up existing remote files to remote_path/previous/{filename}
    before overwriting so we can diff after a bad push.

    Safety: remote_path is validated against ALLOWED_REMOTE_PATHS
    before any connection is opened. Refuses root, refuses arbitrary
    subfolders. This is the last line of defense against a config
    drift that would let Nexus overwrite the SIS's authoritative
    teachers.csv / students.csv / sections.csv sitting at root.
    """
    import paramiko
    # Enforce the write-safety allowlist BEFORE opening any connection.
    remote_path = _validate_remote_path(remote_path)
    # Defensive: strip any scheme prefix a user might have pasted into
    # the settings field ("sftp://sftp2.clever.com" → "sftp2.clever.com").
    # paramiko.Transport wants a bare hostname; the scheme fails DNS.
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.rstrip("/")
    result: dict[str, str] = {}
    transport = paramiko.Transport((host, port))
    try:
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            # Ensure remote_path + previous/ subdir exist. Auto-create
            # any allowlisted subfolder that's missing — Clever's docs
            # say customsections/ must be created manually, but the
            # customteachers/ fallback isn't documented and we own
            # creating it. Since remote_path already passed
            # _validate_remote_path(), auto-mkdir is safe here.
            try:
                sftp.stat(remote_path)
            except FileNotFoundError:
                try:
                    sftp.mkdir(remote_path)
                except Exception as e:
                    raise RuntimeError(
                        f"Remote path {remote_path!r} does not exist "
                        f"and mkdir failed: {e}. Create the subfolder "
                        f"manually via your SFTP client."
                    )
            previous = f"{remote_path.rstrip('/')}/previous"
            try:
                sftp.stat(previous)
            except FileNotFoundError:
                sftp.mkdir(previous)

            for name, content in files.items():
                remote_target = f"{remote_path.rstrip('/')}/{name}"
                # Backup existing
                try:
                    sftp.stat(remote_target)
                    backup_target = f"{previous}/{name}"
                    # If backup already exists, overwrite (previous
                    # sync's copy) — we only keep one revision back.
                    try:
                        sftp.remove(backup_target)
                    except FileNotFoundError:
                        pass
                    sftp.rename(remote_target, backup_target)
                except FileNotFoundError:
                    pass
                # Upload
                data = content.encode("utf-8")
                with sftp.open(remote_target, "w") as fh:
                    fh.write(data)
                result[name] = sha256_hex(data)
        finally:
            sftp.close()
    finally:
        transport.close()
    return result


# ── Audit ──────────────────────────────────────────────────────────


async def audit_sync(
    db: AsyncSession, *, actor: str, dry_run: bool, result: SyncResult,
) -> None:
    action = "roster.custom_sections.dry_run" if dry_run else "roster.custom_sections.sftp_upload"
    payload = {
        "dry_run": dry_run,
        "tabs_read": result.tabs_read,
        "sections_built": result.sections_built,
        "enrollments_built": result.enrollments_built,
        "matched": result.matched,
        "ambiguous": result.ambiguous,
        "missing_sid": result.missing_sid,
        "invalid_teacher": result.invalid_teacher,
        "custom_teachers_built": result.custom_teachers_built,
        "custom_students_built": result.custom_students_built,
        "files_written": result.files_written,
        "upload_details": result.upload_details,
        "errors": result.errors[:20],
    }
    await db.execute(text("""
        INSERT INTO audit_logs (actor, action, module, target, details, created_at)
        VALUES (:actor, :action, 'roster', :target, CAST(:details AS JSONB), NOW())
    """).bindparams(
        actor=actor, action=action,
        target=f"customsections@{date.today().isoformat()}",
        details=json.dumps(payload),
    ))
