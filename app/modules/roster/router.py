"""
Roster module router — student directory, imports, class lists, guidance queue, exports.

Highest FERPA risk surface in the application.

Rules:
- No student PII on unauthenticated endpoints
- All endpoints require appropriate roster.* permission
- Building-scoped users see only their own school
- Exports: admin-only, audited, field-minimized per T0.4
- Class lists: scoped + audited via audit_classlist_access()
- Guidance queue: building-scoped
- Student photos: NOT implemented (T0.6 blocked)
"""

import csv
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import User
from app.policies.engine import require_action, get_user_permissions, check_permission
from app.audit.service import log_action
from app.audit.student import audit_student_view, audit_student_export, audit_classlist_access
from app.modules.roster import repository as repo
from app.modules.roster.schemas import (
    RosterImportRequest, GuidanceQueueCreate, GuidanceQueueUpdate,
    StudentAccountProvision, ConfirmStudentIdRequest, IgnoreEmailRequest,
    ResolveDuplicateRequest, RevertGoogleRequest,
)
from app.policies.page_context import build_page_modules

logger = logging.getLogger(__name__)
router = APIRouter(tags=["roster"])
templates = Jinja2Templates(directory="app/templates")


# ── Semester helpers ──────────────────────────────────────────────────────
# SIS imports both semesters as separate sections. The section /
# course-name suffix `A` maps to SEM1 (fall) and `B` maps to SEM2
# (spring). Year-long courses have no suffix and always show.
#
# Ideally we'd read the term_name field directly from Clever
# (values: 'year', 'SEM1', 'SEM2') — TODO: add term_name column to
# student_teachers and populate from the sections CSV. Until then this
# heuristic covers the common case cleanly since the SIS is 100%
# consistent about A/B suffixes on paired courses.

from sqlalchemy import func, text as _text


async def _current_semester_letter(db: AsyncSession) -> str | None:
    """Determine active semester. Preference order:
      1. Today's ``grading_period`` in ``district_calendar_dates`` — most
         authoritative when populated. Q1/Q2 → 'A' (SEM1),
         Q3/Q4 → 'B' (SEM2).
      2. Month-based fallback for gaps in the calendar (in-service
         days between grading periods, or years the calendar hasn't
         been loaded for): Aug-Dec → 'A', Jan-May → 'B'.
      3. Summer months (Jun/Jul) → None so callers show all sections."""
    row = (await db.execute(_text(
        "SELECT grading_period FROM district_calendar_dates "
        "WHERE date = CURRENT_DATE"
    ))).first()
    if row and row.grading_period:
        gp = row.grading_period.upper()
        if gp in ("Q1", "Q2"): return "A"
        if gp in ("Q3", "Q4"): return "B"

    # Month-based fallback
    from datetime import date as _date
    m = _date.today().month
    if 8 <= m <= 12: return "A"
    if 1 <= m <= 5:  return "B"
    return None  # Jun/Jul → summer → show everything


def _semester_suffix(name: str | None) -> str | None:
    """Return 'A' or 'B' if the course/section name ends in that letter
    at a plausible semester-suffix position (preceded by a digit or
    whitespace), else None. Avoids stripping trailing letters from
    words like 'DRAMA' or names like 'MOZZARELLA'."""
    if not name:
        return None
    s = name.rstrip("/").strip()
    if len(s) < 2:
        return None
    last = s[-1].upper()
    if last not in ("A", "B"):
        return None
    prev = s[-2]
    if prev.isspace() or prev.isdigit():
        return last
    return None


_LETTER_TO_TERM = {"A": "SEM1", "B": "SEM2"}


def _filter_active_semester(sections: list[dict], active: str | None) -> list[dict]:
    """Keep only sections that match the active semester letter.

    Uses the SIS-authoritative ``term`` field first (Clever's
    ``Term_name`` — 'year', 'SEM1', 'SEM2', trimester variants). Falls
    back to the course-name A/B suffix heuristic when ``term`` is
    empty (older rows imported before term capture was added).

    Rules:
      - term == 'year' (case-insensitive) → always show
      - term matches active semester → show
      - term is a different semester → drop
      - term empty → heuristic: if course ends in A/B AND both halves
        exist in this schedule, filter to the active letter's half;
        otherwise show (year-long or one-half elective)
    """
    if not active:
        return sections

    active_term = _LETTER_TO_TERM.get(active.upper())

    from collections import defaultdict
    letters_by_base: dict[str, set[str]] = defaultdict(set)
    for sec in sections:
        if sec.get("term"):
            continue  # SIS term wins — no need for heuristic on this row
        suffix = _semester_suffix(sec.get("course"))
        if suffix:
            base = (sec["course"] or "")[:-1].rstrip()
            letters_by_base[base].add(suffix)

    out = []
    for sec in sections:
        term = (sec.get("term") or "").strip().upper()
        if term:
            # Authoritative path — use SIS term.
            if term == "YEAR":
                out.append(sec)
            elif term == active_term:
                out.append(sec)
            # else: drop (opposite semester or unknown term string —
            # log-worthy if we start seeing unexpected values)
            continue

        # Heuristic path — course-name suffix.
        suffix = _semester_suffix(sec.get("course"))
        if not suffix:
            out.append(sec)
            continue
        base = (sec["course"] or "")[:-1].rstrip()
        pair = letters_by_base.get(base, set())
        if "A" in pair and "B" in pair:
            if suffix == active.upper():
                out.append(sec)
        else:
            out.append(sec)
    return out


# ── Helpers ───────────────────────────────────────────────────────────────

async def _get_user_building_scope(db: AsyncSession, user_id: int) -> str | None:
    """
    Return the building scope for a non-admin user, or None for district-wide.

    Only considers roles that have roster permissions. A user with both
    'hr' (district) and 'principal' (school-scoped) will be scoped to
    their school for roster data, because 'hr' doesn't grant roster access.
    """
    from app.db.models import UserRole, Role, RolePermission, Permission
    from sqlalchemy import select

    # Get roles that have any roster permission
    roster_role_ids = (await db.execute(
        select(RolePermission.role_id).join(Permission).where(
            Permission.action.like("roster.%")
        ).distinct()
    )).scalars().all()
    roster_role_ids = set(roster_role_ids)

    roles = (await db.execute(
        select(UserRole).where(UserRole.user_id == user_id)
    )).scalars().all()

    # Only consider roles that actually grant roster access
    relevant = [r for r in roles if r.role_id in roster_role_ids]

    for role in relevant:
        if role.scope_type == "district" and role.scope_value == "*":
            return None  # District-wide roster access
        if role.scope_type == "school" and role.scope_value:
            return role.scope_value

    return "__no_scope__"  # No roster-relevant scope

async def _can_view_student_contacts(
    db: AsyncSession,
    user: User,
    student,
) -> bool:
    """
    T4 contact access is gated on roster.students.contacts.view with scope.

    Allowed scope patterns:
    - district / *         → admin (all students)
    - school / <code>      → principal, guidance (own building)
    - teacher / <email>    → teacher (own sections via student_teachers join)
    - section / <name>     → section-scoped access

    Returns False if the user has no contacts permission at all.
    """
    permissions = await get_user_permissions(db, user.id)

    for p in permissions:
        if p["action"] != "roster.students.contacts.view":
            continue

        scope_type = p.get("scope_type")
        scope_value = p.get("scope_value")

        if scope_type == "district" and scope_value == "*":
            return True

        if scope_type == "school" and scope_value == student.school:
            return True

        if scope_type in {"teacher", "section"}:
            if await repo.student_matches_contact_scope(
                db,
                student_id=student.id,
                scope_type=scope_type,
                scope_value=scope_value,
            ):
                return True

    return False


async def _get_classlist_access_context(
    db: AsyncSession,
    user: User,
    requested_school: str | None,
) -> dict:
    """
    Resolve class list scope in two passes so contact access does not depend on
    permission row order.

    Section scope must be bound to a school using the format:
        "<school_code>:<section_name>"
    Example:
        "PHS:Math 1"

    Unqualified section-only scope values are rejected because section names are
    not globally unique across buildings.
    """
    permissions = await get_user_permissions(db, user.id)

    effective_school = None
    teacher_email = None
    effective_section = None
    include_contacts = False

    def _parse_section_scope(value: str | None) -> tuple[str | None, str | None]:
        if not value or ":" not in value:
            return None, None
        school_code, section = value.split(":", 1)
        school_code = school_code.strip() or None
        section = section.strip() or None
        return school_code, section

    # Pass 1: resolve class list scope
    for p in permissions:
        if p["action"] != "roster.classlist.view":
            continue

        scope_type = p.get("scope_type")
        scope_value = p.get("scope_value")

        if scope_type == "district" and scope_value == "*":
            effective_school = requested_school
        elif scope_type == "school" and scope_value:
            effective_school = scope_value
        elif scope_type == "teacher" and scope_value:
            teacher_email = scope_value
            effective_school = requested_school
        elif scope_type == "section" and scope_value:
            section_school, section_name = _parse_section_scope(scope_value)
            if section_school and section_name:
                effective_school = section_school
                effective_section = section_name

    # Pass 2: resolve contact overlay permission
    for p in permissions:
        if p["action"] != "roster.students.contacts.view":
            continue

        scope_type = p.get("scope_type")
        scope_value = p.get("scope_value")

        if scope_type == "district" and scope_value == "*":
            include_contacts = True
        elif scope_type == "school" and effective_school and scope_value == effective_school:
            include_contacts = True
        elif scope_type == "teacher" and teacher_email and scope_value == teacher_email:
            include_contacts = True
        elif scope_type == "section" and effective_school and effective_section:
            section_school, section_name = _parse_section_scope(scope_value)
            if section_school == effective_school and section_name == effective_section:
                include_contacts = True

    return {
        "effective_school": effective_school,
        "teacher_email": teacher_email,
        "effective_section": effective_section,
        "include_contacts": include_contacts,
    }


@router.get("/roster", response_class=HTMLResponse)
async def roster_page(
    request: Request,
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    permissions = await get_user_permissions(db, user.id)

    modules = build_page_modules(permissions)

    can_execute = check_permission(permissions, "roster.accounts.provision")
    return templates.TemplateResponse("roster.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "can_execute": can_execute,
    })


@router.get("/roster/clever-verify", response_class=HTMLResponse)
async def clever_verify_page(
    request: Request,
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    """Manual Clever CSV upload + audit against current roster_snapshots.
    Read-only: nothing on the roster is mutated, only roster_clever_verify
    gets upserted so the badge on /roster reflects the latest check."""
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    return templates.TemplateResponse("clever_verify.html", {
        "request": request,
        "user": user,
        "modules": modules,
    })


@router.post("/api/roster/clever-verify/upload")
async def clever_verify_upload(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    """Accept a Clever Students CSV, compare row-by-row to
    roster_snapshots, upsert the verify table, return a summary +
    per-student diff. NEVER writes to roster_snapshots."""
    from app.modules.roster.clever_service import parse_students_csv
    from app.modules.settings.repository import get_setting_value

    # Read + parse
    content = (await file.read()).decode("utf-8", errors="replace")
    students = parse_students_csv(content)
    if not students:
        raise HTTPException(status_code=400, detail="Could not parse any student rows from CSV")

    # Load current roster into memory for comparison. Small enough (~1800
    # active students) to keep the compare simple.
    rows = (await db.execute(_text("""
        SELECT sis_id, first_name, last_name, email, grade, school
        FROM roster_snapshots
        WHERE status = 'active'
    """))).mappings().all()
    by_sis = {r["sis_id"]: dict(r) for r in rows}

    # SIS school code → internal building code (Clever CSV carries the
    # SIS code; roster_snapshots also stores SIS code, so this map is
    # only used when we render mismatch reasons in a human way).
    bmap_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        bmap = json.loads(bmap_raw)
    except (json.JSONDecodeError, TypeError):
        bmap = {}

    def _norm(v):
        """Whitespace-collapse + casefold for lenient comparison."""
        return " ".join(str(v or "").strip().split()).casefold()

    def _norm_grade(v):
        """Canonicalize a grade value so different-format inputs collapse
        to the same key. Handles the district conventions in
        district_grade_codes doc — KG/K/Kindergarten all → "K",
        PK/PS/PreK/Preschool → "PK", "23" → "23" (SpEd bucket kept
        distinct), numerics zero-stripped."""
        s = str(v or "").strip().casefold()
        if not s:
            return ""
        if s in ("k", "kg", "kindergarten", "0"):
            return "k"
        if s in ("pk", "prek", "pre-k", "pre k",
                 "pre kindergarten", "pre-kindergarten",
                 "ps", "preschool"):
            return "pk"
        # numeric — strip leading zeros so "05" == "5"
        if s.isdigit():
            n = int(s)
            return str(n)
        return s

    import re as _re
    _MONGO_OID = _re.compile(r"^[a-f0-9]{24}$")

    def _incompatible_identifier(sis_val, roster_val) -> bool:
        """Return True when we're comparing values from incompatible
        identifier spaces (Clever platform's internal Mongo ObjectId vs
        our SIS school code, for example). The Clever dashboard Students
        export gives `school` as a 24-char hex ObjectId while Nexus
        stores SIS codes like `SIS_A`/`SIS_B`/`EPE`/`SIS_C` — different
        alphabets, no signal in comparing them. MetaSolutions email
        format DOES align (both sides = SIS code), so this check only
        skips the noisy Clever-dashboard case."""
        s = str(sis_val or "").strip()
        r = str(roster_val or "").strip()
        return bool(_MONGO_OID.match(s.lower()) and not _MONGO_OID.match(r.lower()))

    now = datetime.now(timezone.utc)
    summary = {
        "total_in_csv": len(students),
        "matched": 0,
        "mismatched": 0,
        "not_in_roster": 0,
        "checked_at": now.isoformat(),
    }
    diffs: list[dict] = []

    for s in students:
        sid = s.get("sis_id") or s.get("student_id") or ""
        if not sid:
            continue
        rr = by_sis.get(sid)
        if not rr:
            summary["not_in_roster"] += 1
            diffs.append({
                "sis_id": sid,
                "name": f"{s.get('first_name','')} {s.get('last_name','')}".strip(),
                "matched": False,
                "reason": "not_in_roster",
                "mismatches": [],
            })
            # Persist as mismatched with a single "not in roster" note
            await db.execute(_text("""
                INSERT INTO roster_clever_verify
                  (sis_id, matched, mismatches, checked_at, checked_by, source_filename)
                VALUES (:sid, false, CAST(:mm AS JSONB), :now, :by, :src)
                ON CONFLICT (sis_id) DO UPDATE SET
                  matched = EXCLUDED.matched,
                  mismatches = EXCLUDED.mismatches,
                  checked_at = EXCLUDED.checked_at,
                  checked_by = EXCLUDED.checked_by,
                  source_filename = EXCLUDED.source_filename
            """).bindparams(
                sid=sid, mm=json.dumps([{"field": "roster_snapshots",
                                          "sis_value": "present in Clever",
                                          "roster_value": "no active row"}]),
                now=now, by=user.email, src=file.filename,
            ))
            continue

        # Field-by-field compare — normalized. Only compare fields where
        # both sides carry meaningful values; skip the field otherwise
        # (an empty roster email doesn't mean Clever's is "wrong").
        mismatches: list[dict] = []
        for field, sis_val in [
            ("first_name", s.get("first_name")),
            ("last_name", s.get("last_name")),
            ("email", s.get("email")),
            ("grade", s.get("grade")),
            ("school", s.get("school")),
        ]:
            roster_val = rr.get(field)
            # Skip when roster doesn't have the field at all — this is
            # a data-completeness issue on our side, not a Clever drift.
            if not roster_val:
                continue
            # Skip when the two systems disagree on identifier format
            # (Clever's internal Mongo OID vs SIS code for school).
            if _incompatible_identifier(sis_val, roster_val):
                continue
            # Grade needs district-code normalization, not just casefold.
            if field == "grade":
                if _norm_grade(sis_val) == _norm_grade(roster_val):
                    continue
            elif _norm(sis_val) == _norm(roster_val):
                continue
            # If we got here the values genuinely diverge — record it.
            mismatches.append({
                "field": field,
                "sis_value": (str(sis_val) if sis_val is not None else "").strip(),
                "roster_value": str(roster_val).strip(),
            })

        matched = not mismatches
        if matched:
            summary["matched"] += 1
        else:
            summary["mismatched"] += 1
            diffs.append({
                "sis_id": sid,
                "name": f"{rr.get('first_name','')} {rr.get('last_name','')}".strip(),
                "matched": False,
                "reason": "field_mismatch",
                "mismatches": mismatches,
            })

        await db.execute(_text("""
            INSERT INTO roster_clever_verify
              (sis_id, matched, mismatches, checked_at, checked_by, source_filename)
            VALUES (:sid, :m, CAST(:mm AS JSONB), :now, :by, :src)
            ON CONFLICT (sis_id) DO UPDATE SET
              matched = EXCLUDED.matched,
              mismatches = EXCLUDED.mismatches,
              checked_at = EXCLUDED.checked_at,
              checked_by = EXCLUDED.checked_by,
              source_filename = EXCLUDED.source_filename
        """).bindparams(
            sid=sid, m=matched, mm=json.dumps(mismatches),
            now=now, by=user.email, src=file.filename,
        ))

    await log_action(
        db, actor=user.email, action="roster.clever_verify.upload",
        module="roster", target=file.filename or "(no filename)",
        details=json.dumps(summary),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    # Trim diff list for response — big uploads with lots of drift
    # would otherwise return a massive JSON. Full detail stays in the
    # DB for the badge + a mismatch report page.
    return {
        "summary": summary,
        "mismatches": diffs[:200],
        "truncated": len(diffs) > 200,
    }


@router.get("/guidance")
async def guidance_redirect():
    """Redirect to roster page — guidance queue is in the Guidance Queue tab."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/roster", status_code=302)


# ── API: Student directory ───────────────────────────────────────────────

@router.get("/api/roster/students")
async def list_students(
    school: str | None = None,
    grade: str | None = None,
    status: str = "active",
    search: str | None = None,
    issue: str | None = None,
    days: int | None = None,
    show_unenrolled: bool = True,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    request: Request = None,
    user: User = Depends(require_action("roster.students.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Student directory. Building-scoped for principals/guidance.
    By default, only shows students with section assignments (enrolled).
    """
    building_scope = await _get_user_building_scope(db, user.id)
    effective_school = building_scope or school

    # The "changes" filter inherently includes withdrawals (status=inactive);
    # don't let the default status='active' hide them.
    effective_status = None if issue == "changes" else status

    # New enrollments and change-log rows haven't had their Clever section
    # assignments propagated yet — the enrolled_only default (requires at
    # least one row in student_teachers) filters them out. Bypass that
    # gate for those filters so they actually show what the user asked for.
    effective_enrolled_only = (
        False if issue in ("new", "changes") else (not show_unenrolled)
    )

    students, total = await repo.search_students(
        db,
        school=effective_school,
        enrolled_only=effective_enrolled_only,
        grade=grade,
        status=effective_status,
        search=search,
        issue=issue,
        days=days,
        limit=limit,
        offset=offset,
    )

    # Clever-verify enrichment — one small query per page. Nullable
    # fields on the response: `clever_verified` is null when the sis_id
    # was not present in the most recent verify upload; true = matched;
    # false = mismatched (mismatches array carries the field diffs).
    sids_on_page = [s.sis_id for s in students if s.sis_id]
    clever_by_sid: dict[str, dict] = {}
    if sids_on_page:
        cvr = await db.execute(_text("""
            SELECT sis_id, matched, mismatches, checked_at
            FROM roster_clever_verify
            WHERE sis_id = ANY(:sids)
        """).bindparams(sids=sids_on_page))
        for r in cvr.mappings().all():
            clever_by_sid[r["sis_id"]] = {
                "matched": r["matched"],
                "mismatches": r["mismatches"] or [],
                "checked_at": r["checked_at"].isoformat() if r["checked_at"] else None,
            }

    # Attendance enrichment — YTD absence count per sis_id, from
    # student_absences. "School year to date" cutoff = Aug 1 of the
    # current or previous calendar year, whichever's closer in the past
    # (Aug through Dec use current year; Jan through Jul use previous
    # year — matches how US districts number school years).
    absences_by_sid: dict[str, int] = {}
    if sids_on_page:
        from datetime import date as _date
        today = _date.today()
        sy_start = _date(today.year if today.month >= 8 else today.year - 1, 8, 1)
        abr = await db.execute(_text("""
            SELECT sis_id, COUNT(*) AS n
            FROM student_absences
            WHERE sis_id = ANY(:sids) AND calendar_date >= :start
            GROUP BY sis_id
        """).bindparams(sids=sids_on_page, start=sy_start))
        absences_by_sid = {r[0]: int(r[1]) for r in abr.all()}

    # Enrich with homeroom teacher assignments using configured homeroom period per building
    from sqlalchemy import select as sa_sel
    from app.modules.roster.models import StudentTeacher
    from app.modules.settings.repository import get_setting_value
    import json as _hr_json
    hr_periods_raw = await get_setting_value(db, "roster", "homeroom_periods") or "{}"
    try:
        hr_periods = _hr_json.loads(hr_periods_raw)
    except Exception:
        hr_periods = {}

    student_ids = [s.id for s in students]
    # Build school lookup for each student
    student_school = {s.id: s.school for s in students}

    def _norm_period(p):
        """Strip leading zeros for numeric-only period strings so `"01"`
        matches `"1"`. Leaves alpha values (`"HR"`, `"Spec"`) untouched.
        Config stores `"1"` per building but the enrollments CSV writes
        `"01"` — without this normalization homeroom detection silently
        fails for SIS_C + SIS_B (all periods "01"–"09")."""
        if p is None:
            return ""
        s = str(p).strip()
        return s.lstrip("0") if s.isdigit() and len(s) > 1 else s

    # First try: match by configured homeroom period. When multiple
    # teachers share the same period for a student (specialty teacher +
    # primary teacher both in period 1), tie-break by picking the one
    # who has the most sections with the student — that's the true
    # homeroom teacher, not the once-a-week specialty.
    homerooms = {}
    if hr_periods:
        # Count sections per (student, teacher) so we can rank
        section_count_q = await db.execute(
            sa_sel(
                StudentTeacher.student_id, StudentTeacher.teacher_email,
                func.count(StudentTeacher.id).label("n"),
            )
            .where(StudentTeacher.student_id.in_(student_ids))
            .group_by(StudentTeacher.student_id, StudentTeacher.teacher_email)
        )
        sect_count = {(sid, email): n for sid, email, n in section_count_q.all()}

        hr_q = await db.execute(
            sa_sel(StudentTeacher.student_id, StudentTeacher.teacher_name,
                   StudentTeacher.teacher_email, StudentTeacher.period)
            .where(StudentTeacher.student_id.in_(student_ids))
        )
        for sid, tname, temail, period in hr_q.all():
            school = student_school.get(sid, "")
            hr_period_cfg = hr_periods.get(school)
            if not hr_period_cfg or _norm_period(period) != _norm_period(hr_period_cfg):
                continue
            candidate = {"name": tname, "email": temail}
            existing = homerooms.get(sid)
            if not existing:
                homerooms[sid] = candidate
                continue
            # Tie-break: teacher with more sections for this student wins
            new_n = sect_count.get((sid, temail), 0)
            old_n = sect_count.get((sid, existing["email"]), 0)
            if new_n > old_n:
                homerooms[sid] = candidate

    # Fallback: for students without a period-matched homeroom, pick the
    # teacher with the most sections for that student (the "primary"
    # teacher) instead of an arbitrary first row.
    remaining = [sid for sid in student_ids if sid not in homerooms]
    if remaining:
        fb_q = await db.execute(
            sa_sel(
                StudentTeacher.student_id, StudentTeacher.teacher_name,
                StudentTeacher.teacher_email,
                func.count(StudentTeacher.id).label("n"),
            )
            .where(StudentTeacher.student_id.in_(remaining))
            .where(StudentTeacher.teacher_email != "")
            .group_by(StudentTeacher.student_id, StudentTeacher.teacher_name,
                      StudentTeacher.teacher_email)
            .order_by(StudentTeacher.student_id, func.count(StudentTeacher.id).desc())
        )
        for sid, tname, temail, _n in fb_q.all():
            if sid not in homerooms:
                homerooms[sid] = {"name": tname, "email": temail}

    # Enrollment presence — DISTINCT student_id set from student_teachers,
    # scoped to just this page's students. Powers the "No classes" badge
    # so unenrolled kids are easy to spot even in the default view.
    enrolled_ids: set[int] = set()
    if student_ids:
        enrolled_rows = await db.execute(
            sa_sel(StudentTeacher.student_id).distinct()
            .where(StudentTeacher.student_id.in_(student_ids))
        )
        enrolled_ids = {row[0] for row in enrolled_rows.all()}

    # Deprovision-exemption tags. Kid has a non-district email AND no
    # class enrollments → candidate for the deprovision sweep. If they're
    # ALSO in an alt-program (credit recovery, vocational partnership)
    # that doesn't show up in Enrollments.csv, operator can tag them
    # here to skip the sweep.
    from sqlalchemy import text as _sa_text
    sids_this_page = [s.sis_id for s in students if s.sis_id]
    exempted_sids: set[str] = set()
    if sids_this_page:
        er = await db.execute(_sa_text(
            "SELECT sis_id FROM roster_deprov_exemptions "
            "WHERE sis_id = ANY(:sids)"
        ).bindparams(sids=sids_this_page))
        exempted_sids = {r[0] for r in er.all()}

    # Get summary stats — scoped to user's building if applicable
    from sqlalchemy import text as sa_text
    school_filter = "AND school = :school" if effective_school else ""
    params = {"school": effective_school} if effective_school else {}
    total_active = (await db.execute(sa_text(
        f"SELECT count(*) FROM roster_snapshots WHERE status='active' {school_filter}"
    ).bindparams(**params))).scalar_one()
    total_inactive = (await db.execute(sa_text(
        f"SELECT count(*) FROM roster_snapshots WHERE status!='active' {school_filter}"
    ).bindparams(**params))).scalar_one()
    total_unenrolled = (await db.execute(sa_text(
        f"SELECT count(*) FROM roster_snapshots WHERE status='active' AND id NOT IN (SELECT DISTINCT student_id FROM student_teachers) {school_filter}"
    ).bindparams(**params))).scalar_one()
    total_enrolled = (await db.execute(sa_text(
        f"SELECT count(*) FROM roster_snapshots WHERE status='active' AND id IN (SELECT DISTINCT student_id FROM student_teachers) {school_filter}"
    ).bindparams(**params))).scalar_one()
    last_import_row = (await db.execute(sa_text(
        "SELECT started_at FROM roster_imports ORDER BY id DESC LIMIT 1"
    ))).first()
    last_import = last_import_row[0].isoformat() if last_import_row and last_import_row[0] else None

    # School names for display
    import json as _json
    from app.modules.settings.repository import get_setting_value
    school_names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        school_names = _json.loads(school_names_raw)
    except Exception:
        school_names = {}

    # Issue counts
    issue_counts = {"new": 0, "any": 0}
    for s in students:
        tags = (s.issue_tags or "").split(",") if s.issue_tags else []
        if tags:
            issue_counts["any"] += 1

    # When the Changes filter is active, look up each student's most-recent
    # roster_changes row in the day window so the row can show a colored
    # change-type badge. Single keyset query, not per-row.
    recent_change_by_sis: dict[str, str] = {}
    if issue == "changes" and students:
        from app.modules.roster.models import RosterChange
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select as sa_sel
        d = days or 7
        cutoff = datetime.now(timezone.utc) - timedelta(days=d)
        sis_ids = [s.sis_id for s in students if s.sis_id]
        if sis_ids:
            rows = (await db.execute(
                sa_sel(RosterChange.student_id, RosterChange.change_type, RosterChange.created_at)
                .where(
                    RosterChange.student_id.in_(sis_ids),
                    RosterChange.created_at >= cutoff,
                    RosterChange.change_type.in_([
                        "added", "removed", "transferred",
                        "grade_change", "name_change",
                    ]),
                )
                .order_by(RosterChange.created_at.desc())
            )).all()
            for sis, ctype, _ in rows:
                # First (most recent) wins
                recent_change_by_sis.setdefault(sis, ctype)

    await db.commit()  # Persist FERPA audit from require_action

    # Build expected email helper
    from app.modules.roster.clever_service import build_expected_email, expected_grad_year
    from app.modules.settings.repository import get_setting_value
    student_domain = await get_setting_value(db, "google", "student_domain") or await get_setting_value(db, "google", "domain") or ""

    student_list = []
    for s in students:
        sis_email = (s.email or "").strip().lower()
        google_st = (s.google_status or "").strip().lower()
        has_google = google_st in ("active", "provisioned")
        emails_match = bool(sis_email and has_google)  # SIS is source of truth — if they're in Google, email matches
        tags = [t.strip() for t in (s.issue_tags or "").split(",") if t.strip()]
        hr = homerooms.get(s.id)
        is_active = s.status == "active"

        # Build expected email from name + grad year
        grad_year = expected_grad_year(s.grade) if s.grade else None
        exp_email = build_expected_email(s.last_name, s.first_name, grad_year, domain=student_domain) if grad_year and s.last_name and s.first_name else None

        # Compliance issue detail
        compliance_issue = None
        if s.email_compliant is False and s.issue_tags:
            for t in tags:
                if t in ("wrong_domain", "year_mismatch", "format_mismatch", "name_mismatch", "insufficient_data"):
                    compliance_issue = t
                    break

        student_list.append({
            "id": s.id,
            "student_id": s.sis_id,
            "sis_id": s.sis_id,
            "name": f"{s.first_name} {s.last_name}".strip(),
            "first_name": s.first_name,
            "last_name": s.last_name,
            "school": s.school,
            "school_name": school_names.get(s.school, s.school),
            "grade": s.grade,
            "status": s.status,
            "is_active": is_active,
            "is_new": False,  # TODO: flag from recent roster_changes
            "sis_email": sis_email,
            "has_sis_email": bool(sis_email),
            "google_email": sis_email if has_google else None,
            "google_name": None,  # Populated after Google recheck
            "expected_email": exp_email,
            "has_google": has_google,
            "emails_match": emails_match,
            "google_status": s.google_status,
            "email_compliant": s.email_compliant,
            "compliance_issue": compliance_issue,
            "homeroom": hr,
            "has_enrollments": s.id in enrolled_ids,
            "google_ou": s.google_ou,
            "clever_verify": clever_by_sid.get(s.sis_id),
            "absences_ytd": absences_by_sid.get(s.sis_id, 0),
            # Deprovision-sweep flags. `is_deprov_candidate` = the toggle
            # is visible on this row at all (non-district email + no
            # classes). `is_deprov_exempted` = operator has tagged this
            # kid to be skipped by the sweep even though they qualify.
            "is_deprov_candidate": (
                bool(sis_email)
                and not sis_email.endswith("@yourdistrict.org")
                and s.id not in enrolled_ids
                and is_active
            ),
            "is_deprov_exempted": s.sis_id in exempted_sids,
            "issues": tags,
            "issue_tags": s.issue_tags,
            "recent_change": recent_change_by_sis.get(s.sis_id),
        })

    return {
        "total": total,
        "total_active": total_active,
        "total_enrolled": total_enrolled,
        "total_inactive": total_inactive,
        "total_unenrolled": total_unenrolled,
        "last_import": last_import,
        "issue_counts": issue_counts,
        "offset": offset,
        "limit": limit,
        "students": student_list,
    }


@router.get("/api/roster/students/{sis_id}/absences")
async def get_student_absences(
    sis_id: str,
    days: int = Query(365, ge=1, le=1825),
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    """Absence history for one student, most-recent first.

    Default 365d window covers a full school year for the profile
    modal. Higher caps (5y) supported for cross-year audits."""
    from datetime import date as _date, timedelta as _timedelta
    cutoff = _date.today() - _timedelta(days=days)
    rows = (await db.execute(_text("""
        SELECT calendar_date, absence_type, absence_type_name,
               absence_level, absence_reason, absence_note,
               time_in, time_out, comments, grade, homeroom
        FROM student_absences
        WHERE sis_id = :sid AND calendar_date >= :cutoff
        ORDER BY calendar_date DESC, absence_type
    """).bindparams(sid=sis_id, cutoff=cutoff))).mappings().all()
    return {
        "sis_id": sis_id,
        "window_days": days,
        "count": len(rows),
        "absences": [
            {
                "date": r["calendar_date"].isoformat(),
                "type": r["absence_type"],
                "type_name": r["absence_type_name"],
                "level": r["absence_level"],
                "reason": r["absence_reason"],
                "note": r["absence_note"],
                "time_in": r["time_in"],
                "time_out": r["time_out"],
                "comments": r["comments"],
                "grade": r["grade"],
                "homeroom": r["homeroom"],
            }
            for r in rows
        ],
    }


@router.get("/api/roster/students/{student_id}")
async def get_student_detail(
    student_id: str,
    request: Request,
    all_semesters: bool = False,
    user: User = Depends(require_action("roster.students.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Student profile detail. Accepts SIS ID. Building- or assignment-scoped.

    Includes T1+T2+T5 fields by default.
    T4 contact fields (parent/guardian, phone, parent email) are only
    included when the caller has roster.students.contacts.view within
    an allowed district/school/teacher/section scope.

    Audited per FERPA.
    """
    student = await repo.get_student_by_sis_id(db, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Scope enforcement
    building_scope = await _get_user_building_scope(db, user.id)
    if building_scope and student.school != building_scope:
        raise HTTPException(status_code=403, detail="Student not in your assigned building")

    await audit_student_view(
        db,
        actor=user.email,
        view_type="profile",
        student_id=student.sis_id,
        scope=student.school,
        ip_address=request.client.host if request.client else None,
    )

    from app.modules.roster.clever_service import build_expected_email, expected_grad_year
    from app.modules.settings.repository import get_setting_value
    from app.modules.roster.models import StudentTeacher
    import json as _json

    domain = await get_setting_value(db, "google", "student_domain") or ""

    # Build expected email
    grad_year = expected_grad_year(student.grade) if student.grade else None
    exp_email = build_expected_email(student.last_name, student.first_name, grad_year, domain=domain) if grad_year and domain and student.last_name and student.first_name else None

    # School name mapping
    school_names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        school_names = _json.loads(school_names_raw)
    except Exception:
        school_names = {}
    school_name = school_names.get(student.school, student.school)

    # Homeroom — use configured homeroom period for this building
    from sqlalchemy import select as sa_sel
    hr_periods_raw = await get_setting_value(db, "roster", "homeroom_periods") or "{}"
    try:
        hr_periods = _json.loads(hr_periods_raw)
    except Exception:
        hr_periods = {}
    hr_period = hr_periods.get(student.school)

    hr = None
    if hr_period:
        hr_result = await db.execute(
            sa_sel(StudentTeacher.teacher_name, StudentTeacher.teacher_email)
            .where(StudentTeacher.student_id == student.id, StudentTeacher.period == hr_period)
            .limit(1)
        )
        hr = hr_result.first()
    if not hr:
        # Fallback: first teacher with email
        hr_result = await db.execute(
            sa_sel(StudentTeacher.teacher_name, StudentTeacher.teacher_email)
            .where(StudentTeacher.student_id == student.id, StudentTeacher.teacher_email != "")
            .limit(1)
        )
        hr = hr_result.first()

    # Current class from building-specific bell schedule
    current_class = None
    try:
        bells_raw = await get_setting_value(db, "roster", "bell_schedules") or "{}"
        all_schedules = _json.loads(bells_raw)
        bell_schedule = all_schedules.get(student.school, {})
        tz_name = await get_setting_value(db, "branding", "timezone") or "America/New_York"
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        now_local = _dt.now(ZoneInfo(tz_name))
        now_mins = now_local.hour * 60 + now_local.minute

        # Get this student's sections with periods
        period_result = await db.execute(
            sa_sel(StudentTeacher.period, StudentTeacher.teacher_name, StudentTeacher.section_name, StudentTeacher.course_name)
            .where(StudentTeacher.student_id == student.id, StudentTeacher.period != None, StudentTeacher.period != "")
        )
        student_periods = {r[0]: {"teacher": r[1], "section": r[2], "course": r[3] or ""} for r in period_result.all()}

        for period_num, times in bell_schedule.items():
            start_parts = times.get("start", "").split(":")
            end_parts = times.get("end", "").split(":")
            if len(start_parts) == 2 and len(end_parts) == 2:
                start_mins = int(start_parts[0]) * 60 + int(start_parts[1])
                end_mins = int(end_parts[0]) * 60 + int(end_parts[1])
                if start_mins <= now_mins <= end_mins and period_num in student_periods:
                    sp = student_periods[period_num]
                    current_class = {
                        "period": period_num,
                        "teacher": sp["teacher"],
                        "section": sp["section"],
                        "course": sp["course"],
                        "start": times["start"],
                        "end": times["end"],
                    }
                    break
    except Exception as e:
        logger.warning(f"Bell schedule lookup failed: {e}")

    student_data = {
        "student_id": student.sis_id,
        "first_name": student.first_name,
        "last_name": student.last_name,
        "middle_name": student.middle_name,
        "school": student.school,
        "school_name": school_name,
        "grade": student.grade,
        "status": student.status,
        "dob": student.dob,
        "sis_email": (student.email or "").strip(),
        "enrollment_date": student.enrollment_date,
        "withdrawal_date": student.withdrawal_date,
        "homeroom": hr[0] if hr else None,
        "homeroom_teacher": hr[1] if hr else None,
        "current_class": current_class,
    }

    # T4 contacts — always include for authorized users
    if await _can_view_student_contacts(db, user, student):
        student_data["parent_guardian"] = student.parent_guardian
        student_data["phone"] = student.phone
        student_data["address"] = student.address

    # Google info
    google_data = None
    if student.google_status == "active" and student.email:
        google_data = {
            "email": student.email,
            "status": student.google_status,
            "suspended": False,
            "has_student_id": True,  # TODO: check via Google API if needed
        }

    compliance_data = {
        "expected": exp_email,
        "compliant": student.email_compliant,
        "issues": [t.strip() for t in (student.issue_tags or "").split(",") if t.strip()],
    }

    # Sections / teachers
    sections_result = await db.execute(
        sa_sel(StudentTeacher.teacher_name, StudentTeacher.teacher_email,
               StudentTeacher.section_name, StudentTeacher.period,
               StudentTeacher.course_name, StudentTeacher.term_name)
        .where(StudentTeacher.student_id == student.id)
        .order_by(StudentTeacher.period, StudentTeacher.section_name)
    )
    sections = [
        {"teacher": r[0], "email": r[1], "section": r[2],
         "period": r[3] or "", "course": r[4] or "", "term": r[5] or ""}
        for r in sections_result.all()
    ]

    # Active-semester filter — SIS imports both semesters as separate
    # sections (course-name suffix A = fall, B = spring). Auto-detect
    # which one is active from today's grading_period in the district
    # calendar and hide the other. Full-year courses (no A/B suffix,
    # or only one half in the schedule) always show. Set
    # ``?all_semesters=1`` on the URL to bypass the filter.
    if not all_semesters:
        sections = _filter_active_semester(
            sections, await _current_semester_letter(db),
        )

    # Guidance queue
    from app.modules.roster.models import GuidanceQueue
    gq_result = await db.execute(
        sa_sel(GuidanceQueue).where(GuidanceQueue.student_id == student.id).limit(1)
    )
    gq = gq_result.scalar_one_or_none()
    guidance_data = None
    if gq:
        guidance_data = {
            "status": gq.status,
            "counselor": gq.assigned_to or "",
            "scheduled_by": gq.resolved_by,
        }

    # Contacts (from T4 fields if permitted)
    contacts = []
    if student_data.get("parent_guardian"):
        contacts.append({
            "title": "",
            "last_name": student_data["parent_guardian"],
            "home_phone": student_data.get("phone", ""),
            "mobile_phone": "",
            "email": "",
            "street": student_data.get("address", ""),
            "city": "", "state": "", "zip": "",
        })

    # Change history for this student
    from app.modules.roster.models import RosterChange
    changes_result = await db.execute(
        sa_sel(RosterChange.change_type, RosterChange.details, RosterChange.created_at)
        .where(RosterChange.student_id == student.sis_id)
        .order_by(RosterChange.created_at.desc())
        .limit(20)
    )
    changes = [
        {"type": r[0], "details": r[1], "date": r[2].isoformat() if r[2] else None}
        for r in changes_result.all()
    ]

    # Chromebook devices — match by student email in last_user or annotated_user
    devices = []
    student_email = (student.email or "").strip().lower()
    if student_email:
        from sqlalchemy import text as _txt

        # Load known district WAN IPs once so we can flag off-campus devices
        # (same logic as the chromebook directory's off-network filter).
        from app.modules.settings.repository import get_setting_value
        import json as _json
        wan_raw = await get_setting_value(db, "chromebook", "known_wan_ips") or ""
        try:
            known_wans = set(_json.loads(wan_raw))
        except Exception:
            known_wans = {ip.strip() for ip in wan_raw.split(",") if ip.strip()}

        dev_rows = await db.execute(_txt("""
            SELECT c.device_id, c.serial, c.model, c.status, c.last_sync,
                   c.annotated_asset_id, c.annotated_location, c.last_network_address, c.mac_address,
                   w.ap_name AS live_ap,
                   h.ap_name AS hist_ap, h.last_seen AS hist_last_seen,
                   c.last_network_wan
            FROM chromebook_cache c
            LEFT JOIN wireless_client_cache w ON w.ip = c.last_network_address
            LEFT JOIN LATERAL (
                SELECT ap_name, last_seen FROM wireless_client_history
                WHERE replace(mac, ':', '') = lower(replace(c.mac_address, ':', ''))
                ORDER BY last_seen DESC LIMIT 1
            ) h ON true
            WHERE lower(c.last_user) = :email OR lower(c.annotated_user) = :email
            ORDER BY c.last_sync DESC NULLS LAST
            LIMIT 5
        """).bindparams(email=student_email))
        for r in dev_rows.all():
            ap_name = r[9] or r[10]  # live_ap or hist_ap
            ap_live = r[9] is not None
            last_wan = r[12]
            # Off-campus if we have a known-WAN list AND the device's last
            # WAN IP is populated AND it isn't one of our district IPs.
            # If known_wans is empty (unconfigured), we can't tell — don't flag.
            off_campus = bool(
                known_wans and last_wan and last_wan not in known_wans
            )
            devices.append({
                "device_id": r[0],
                "serial": r[1],
                "model": r[2],
                "status": r[3],
                "last_sync": r[4],
                "asset_id": r[5],
                "location": r[6],
                "last_ip": r[7],
                "ap_name": ap_name,
                "ap_live": ap_live,
                "ap_last_seen": r[11].isoformat() if r[11] and not ap_live else None,
                "last_wan": last_wan,
                "off_campus": off_campus,
            })

    # SIS-authoritative membership status (a201). Complements the
    # derived `student.status` — provides real "why is this kid on the
    # inactive list" answer (withdrawn / A/E / juvenile / etc.).
    from app.modules.roster.membership import get_code_map
    ms_row = (await db.execute(_text("""
        SELECT sms.code, sms.district_withdrawal_date,
               sms.district_withdrawal_reason, sms.imported_at,
               EXISTS(SELECT 1 FROM student_attends_our_classes o
                      WHERE o.sis_id = sms.sis_id) AS attends_override
        FROM student_membership_status sms
        WHERE sms.sis_id = :sid
    """).bindparams(sid=student.sis_id))).mappings().first()
    if ms_row:
        code_map = await get_code_map(db)
        entry = code_map.get(ms_row["code"], {})
        membership_data = {
            "code": ms_row["code"],
            "label": entry.get("label") or ms_row["code"],
            "code_enrolled": bool(entry.get("enrolled", False)),
            "auto_deprovision": bool(entry.get("auto_deprovision", False)),
            "attends_our_classes_override": bool(ms_row["attends_override"]),
            "effective_enrolled": (
                bool(entry.get("enrolled", False))
                or bool(ms_row["attends_override"])
            ),
            "district_withdrawal_date": ms_row["district_withdrawal_date"],
            "district_withdrawal_reason": ms_row["district_withdrawal_reason"],
            "imported_at": ms_row["imported_at"].isoformat() if ms_row["imported_at"] else None,
        }
    else:
        membership_data = None

    await db.commit()  # Persist FERPA audit entry from audit_student_view
    return {
        "student": student_data,
        "google": google_data,
        "compliance": compliance_data,
        "sections": sections,
        "guidance": guidance_data,
        "contacts": contacts,
        "changes": changes,
        "devices": devices,
        "membership": membership_data,
    }


# ── API: Exports ─────────────────────────────────────────────────────────

@router.get("/api/roster/export")
async def export_students(
    school: str | None = None,
    status: str = "active",
    request: Request = None,
    user: User = Depends(require_action("roster.students.export")),
    db: AsyncSession = Depends(get_db),
):
    """
    Export student data as CSV. Admin-only, audited, field-minimized per T0.4.
    """
    from app.modules.roster.service import export_students_csv

    csv_data = await export_students_csv(
        db,
        school=school,
        status=status,
        actor=user.email,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=roster_export.csv"},
    )


# ── API: Class lists ────────────────────────────────────────────────────

@router.get("/api/roster/classlist")
async def get_classlist(
    school: str | None = None,
    teacher_name: str | None = None,
    section_name: str | None = None,
    request: Request = None,
    user: User = Depends(require_action("roster.classlist.view")),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _get_classlist_access_context(db, user, school)
    effective_school = ctx["effective_school"]
    teacher_email = ctx["teacher_email"]
    scoped_section = ctx["effective_section"]
    include_contacts = ctx["include_contacts"]

    requested_section = section_name or scoped_section

    if not effective_school and not teacher_email and not scoped_section:
        raise HTTPException(
            status_code=400,
            detail="School, teacher, or section scope is required for class list view",
        )

    # Section-scoped users are bound to one school via "<school>:<section>".
    if scoped_section:
        if not effective_school:
            raise HTTPException(
                status_code=400,
                detail="Section scope must include school context",
            )
        if school and school != effective_school:
            raise HTTPException(
                status_code=403,
                detail="Requested school does not match your section scope",
            )
        if section_name and section_name != scoped_section:
            raise HTTPException(
                status_code=403,
                detail="Requested section does not match your assigned section scope",
            )

    await audit_classlist_access(
        db,
        actor=user.email,
        building=effective_school or (f"teacher:{teacher_email}" if teacher_email else f"section:{requested_section}"),
        ip_address=request.client.host if request.client else None,
    )

    class_list = await repo.get_class_list(
        db,
        school=effective_school,
        teacher_email=teacher_email,
        teacher_name=teacher_name,
        section_name=requested_section,
        include_contacts=include_contacts,
    )

    await db.commit()

    return {
        "school": effective_school,
        "total": len(class_list),
        "students": class_list,
    }



# ── API: Imports ─────────────────────────────────────────────────────────

@router.post("/api/roster/import")
async def trigger_import(
    body: RosterImportRequest,
    request: Request,
    user: User = Depends(require_action("roster.import.run")),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger a roster import. The import record is committed before enqueue so
    the worker can always resolve import_id safely.
    """
    imp = await repo.create_import(
        db,
        source=body.source,
        started_by=user.email,
        filename=body.filename,
    )

    # Commit the import record first so the worker never races a missing row.
    await db.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings

        settings = get_settings()
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        job = await redis.enqueue_job(
            "run_roster_import",
            imp.id,
            _job_id=f"roster_import:{imp.id}",
        )

        if not job:
            await log_action(
                db,
                actor=user.email,
                action="roster.import.enqueue_skipped",
                module="roster",
                target=f"import_{imp.id}",
                details="duplicate_job_id",
                ip_address=request.client.host if request.client else None,
            )
            await db.commit()
            return {
                "status": "already_queued",
                "import_id": imp.id,
            }

    except Exception as e:
        # Re-open failure state on the already-persisted import row.
        imp = await repo.get_import(db, imp.id)
        if imp:
            imp.status = "failed"
            imp.error_detail = f"ARQ enqueue failed: {str(e)[:200]}"

        await log_action(
            db,
            actor=user.email,
            action="roster.import.enqueue_failed",
            module="roster",
            target=f"import_{imp.id if imp else 'unknown'}",
            details=str(e)[:200],
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail="Worker queue unavailable — import not started",
        )

    try:
        await log_action(
            db,
            actor=user.email,
            action="roster.import.run",
            module="roster",
            target=f"import_{imp.id}",
            details=f"source={body.source}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Post-enqueue import audit commit failed for import {imp.id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Import job was queued, but final logging failed. Import ID: {imp.id}",
        )

    return {"status": "ok", "import_id": imp.id}


@router.post("/api/roster/upload-csv")
async def upload_clever_csv(
    request: Request,
    user: User = Depends(require_action("roster.import.run")),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload Clever CSV files directly. Auto-detects type from headers.
    Processes through the same pipeline as Gmail-polled imports.
    Supports multiple files in one upload for cross-referencing.
    """
    form = await request.form()
    files = []
    for key in form:
        f = form[key]
        if hasattr(f, "read"):
            content = await f.read()
            files.append({"filename": f.filename, "content": content.decode("utf-8-sig")})

    if not files:
        raise HTTPException(status_code=400, detail="No CSV files provided")

    from app.workers.clever_import_job import _process_csv, _build_student_teacher_map, _enrich_sections

    collected_teachers = {}
    collected_enrollments = []
    collected_section_meta = {}
    processed = 0
    errors = []

    for f in files:
        try:
            import_type = f["filename"].lower().replace(".csv", "").replace("clever-", "").replace("clever_", "")
            teachers, enrollments, section_meta = await _process_csv(
                db, f["content"], import_type, f["filename"],
            )
            if teachers:
                collected_teachers.update(teachers)
            if enrollments:
                collected_enrollments.extend(enrollments)
            if section_meta:
                collected_section_meta.update(section_meta)
            processed += 1
        except Exception as e:
            errors.append(f"{f['filename']}: {str(e)[:100]}")

    # Cross-reference enrollments with teachers
    if collected_enrollments and collected_teachers:
        await _build_student_teacher_map(db, collected_enrollments, collected_teachers)

    # Enrich sections after map is built
    if collected_section_meta:
        await _enrich_sections(db, collected_section_meta)

    await db.commit()

    await log_action(
        db,
        actor=user.email,
        action="roster.import.upload",
        module="roster",
        target=f"{processed} file(s)",
        details=f"files={[f['filename'] for f in files]}, errors={errors}" if errors else f"files={[f['filename'] for f in files]}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return {
        "status": "ok",
        "processed": processed,
        "files": [f["filename"] for f in files],
        "teachers": len(collected_teachers),
        "enrollments": len(collected_enrollments),
        "sections": len(collected_section_meta),
        "errors": errors,
    }


@router.get("/api/roster/imports")
async def list_imports(
    user: User = Depends(require_action("roster.import.run")),
    db: AsyncSession = Depends(get_db),
):
    imports = await repo.list_imports(db)
    return [
        {
            "id": i.id,
            "source": i.source,
            "status": i.status,
            "total_records": i.total_records,
            "added": i.added,
            "updated": i.updated,
            "removed": i.removed,
            "errors": i.errors,
            "started_by": i.started_by,
            "started_at": i.started_at.isoformat() if i.started_at else None,
            "completed_at": i.completed_at.isoformat() if i.completed_at else None,
        }
        for i in imports
    ]


# ── API: Guidance queue ──────────────────────────────────────────────────

@router.get("/api/roster/guidance/queue")
async def list_guidance_queue(
    school: str | None = None,
    status: str | None = "pending",
    category: str | None = None,
    assigned_to: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    request: Request = None,
    user: User = Depends(require_action("roster.guidance.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Guidance queue — building-scoped for counselors.

    Vocabulary bridge: the UI uses ``pending`` / ``scheduled`` /
    ``cancelled`` / ``all`` as the user-facing queue states, while the
    database stores ``open`` / ``in_progress`` / ``resolved`` /
    ``deferred``. Translation lives here so the rest of the codebase
    (including ``auto_queue_new_enrollments`` and the schedule/cancel
    endpoints) can keep using the DB vocabulary, and the UI doesn't
    have to know about the internal state machine.

    Response shape is built for the UI's ``loadGuidanceQueue()``:
    ``entries`` (not items), plus ``pending_count`` for the tab badge.
    Every entry carries the enriched fields the table renders —
    ``student_name``, ``school_name`` (internal building code),
    ``counselor_name``, ``google_email``, ``scheduled_by``.
    """
    building_scope = await _get_user_building_scope(db, user.id)

    # Admin / district-wide → no school filter unless explicitly passed.
    # Non-admin with __no_scope__ → show nothing (never reveal other
    # buildings' data).
    if building_scope == "__no_scope__":
        await db.commit()
        return {
            "total": 0,
            "pending_count": 0,
            "offset": offset,
            "limit": limit,
            "entries": [],
        }

    effective_school = building_scope or (school if school and school != "ALL" else None)

    # UI status → DB status list
    ui_status = (status or "pending").lower()
    if ui_status == "pending":
        db_statuses = ["open", "in_progress"]
    elif ui_status == "scheduled":
        db_statuses = ["resolved"]
    elif ui_status == "cancelled":
        db_statuses = ["deferred"]
    elif ui_status == "all":
        db_statuses = None  # no filter
    else:
        # Allow raw DB values too for API callers.
        db_statuses = [ui_status]

    # Query with translated status list. The repo helper only accepts a
    # single status, so we inline the query here to support the list.
    from app.modules.roster.models import GuidanceQueue, RosterSnapshot
    from sqlalchemy import select as _qs, func as _func, desc as _desc

    # School filter — the queue stores SIS codes (SIS_A, SIS_B, SIS_C) but
    # a school-scoped role might be set to the internal code (PES, PHS,
    # EPE). Expand the effective_school to include both sides of the
    # bmap so a PES principal actually sees SIS_A queue rows.
    school_codes: list[str] | None = None
    if effective_school:
        from app.modules.settings.repository import get_setting_value as _get_sv
        bmap_raw_early = await _get_sv(db, "branding", "school_building_map") or "{}"
        try:
            bmap_early = json.loads(bmap_raw_early)
        except (json.JSONDecodeError, TypeError):
            bmap_early = {}
        up = (effective_school or "").upper()
        codes_set = {effective_school}
        # If it's an internal code, add every SIS code that maps to it.
        for sis, internal in bmap_early.items():
            if (internal or "").upper() == up:
                codes_set.add(sis)
            if (sis or "").upper() == up:
                codes_set.add(internal)
        school_codes = [c for c in codes_set if c]

    # Codes marked auto_deprovision=true (defaults: I + D) are entirely
    # inactive students. They shouldn't appear in the queue at all —
    # counselors were being flooded with rows for kids who were deleted
    # or withdrawn per the SIS.
    from app.modules.roster.membership import get_code_map
    _code_map = await get_code_map(db)
    _hide_codes = [c for c, e in _code_map.items() if e.get("auto_deprovision", False)]
    _hide_sids: list[str] = []
    if _hide_codes:
        _hide_sids = list((await db.execute(_text("""
            SELECT sis_id FROM student_membership_status
            WHERE code = ANY(CAST(:c AS text[]))
        """).bindparams(c=_hide_codes))).scalars().all())

    def _apply_common(qy):
        if school_codes:
            qy = qy.where(GuidanceQueue.school.in_(school_codes))
        if db_statuses is not None:
            qy = qy.where(GuidanceQueue.status.in_(db_statuses))
        if assigned_to:
            qy = qy.where(GuidanceQueue.assigned_to == assigned_to)
        if _hide_sids:
            # RosterSnapshot.sis_id ↔ GuidanceQueue.student_id via the id
            # column on RosterSnapshot; use a subquery to find the internal
            # ids we want to exclude.
            qy = qy.where(
                GuidanceQueue.student_id.notin_(
                    _qs(RosterSnapshot.id).where(RosterSnapshot.sis_id.in_(_hide_sids))
                )
            )
        # Also hide entries whose student is roster_snapshots.status='inactive'
        # — catches SIS-duplicate phantoms (kid re-entered with a new SID,
        # old row went inactive but a stale queue entry lingers) and any
        # A/E-ish kid missing from the membership report entirely.
        qy = qy.where(
            GuidanceQueue.student_id.notin_(
                _qs(RosterSnapshot.id).where(RosterSnapshot.status == "inactive")
            )
        )
        return qy

    q = _apply_common(_qs(GuidanceQueue))
    # Category filter — 'enrollment' | 'withdrawal' | 'transfer' | None.
    # Split-view UI: withdrawals accumulated to 1000+ open because
    # guidance wasn't acking them, drowning out enrollments-needing-
    # action. Filtering by category lets each list be worked in
    # isolation.
    if category:
        q = q.where(GuidanceQueue.category == category)

    count_q = _qs(_func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    # Per-category breakdown for the sub-nav badges. Same common
    # filters as the main query so counts match the visible rows;
    # excludes the `category` filter itself (that's what the badges
    # select FROM).
    cat_q = _apply_common(
        _qs(GuidanceQueue.category, _func.count()).group_by(GuidanceQueue.category)
    )
    counts_by_cat = {c: n for c, n in (await db.execute(cat_q)).all()}

    items = list(
        (
            await db.execute(
                q.order_by(_desc(GuidanceQueue.created_at))
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # Separate query for the pending-count badge so the tab badge
    # doesn't jump around when the user filters to Scheduled / All.
    # Excludes the same auto_deprovision hide-list as the main query
    # so the badge matches what the user actually sees.
    pending_inner = _qs(GuidanceQueue.id).where(
        GuidanceQueue.status.in_(["open", "in_progress"])
    )
    if school_codes:
        pending_inner = pending_inner.where(GuidanceQueue.school.in_(school_codes))
    if _hide_sids:
        pending_inner = pending_inner.where(
            GuidanceQueue.student_id.notin_(
                _qs(RosterSnapshot.id).where(RosterSnapshot.sis_id.in_(_hide_sids))
            )
        )
    pending_inner = pending_inner.where(
        GuidanceQueue.student_id.notin_(
            _qs(RosterSnapshot.id).where(RosterSnapshot.status == "inactive")
        )
    )
    pending_count = (
        await db.execute(_qs(_func.count()).select_from(pending_inner.subquery()))
    ).scalar_one()

    # Enrich with student names, grades, emails.
    student_ids = [item.student_id for item in items]
    student_map: dict[int, dict] = {}
    if student_ids:
        _students = await db.execute(
            _qs(
                RosterSnapshot.id,
                RosterSnapshot.first_name,
                RosterSnapshot.last_name,
                RosterSnapshot.grade,
                RosterSnapshot.sis_id,
                RosterSnapshot.email,
            ).where(RosterSnapshot.id.in_(student_ids))
        )
        for r in _students.all():
            student_map[r[0]] = {
                "name": f"{r[1] or ''} {r[2] or ''}".strip(),
                "grade": r[3] or "",
                "sis_id": r[4] or "",
                "email": r[5] or "",
            }
        # Enrich with membership status so counselors can see A / R / F /
        # CTC / etc. right in the queue table — critical for deciding
        # whether an enrollment row actually needs a schedule.
        sis_ids = [s["sis_id"] for s in student_map.values() if s.get("sis_id")]
        if sis_ids:
            ms_rows = (await db.execute(_text("""
                SELECT sms.sis_id, sms.code,
                       EXISTS(SELECT 1 FROM student_attends_our_classes ao
                              WHERE ao.sis_id = sms.sis_id) AS attends_override
                FROM student_membership_status sms
                WHERE sms.sis_id = ANY(CAST(:s AS text[]))
            """).bindparams(s=sis_ids))).mappings().all()
            from app.modules.roster.membership import get_code_map
            code_map = await get_code_map(db)
            ms_by_sid = {r["sis_id"]: r for r in ms_rows}
            for sinfo in student_map.values():
                ms = ms_by_sid.get(sinfo.get("sis_id"))
                if not ms:
                    sinfo["membership_code"] = None
                    sinfo["membership_label"] = None
                    sinfo["membership_enrolled"] = None
                    continue
                entry = code_map.get(ms["code"], {})
                sinfo["membership_code"] = ms["code"]
                sinfo["membership_label"] = entry.get("label") or ms["code"]
                sinfo["membership_enrolled"] = (
                    bool(entry.get("enrolled", False)) or bool(ms["attends_override"])
                )

    # Load counselor group config so we can resolve counselor_name per row.
    from app.modules.settings.repository import get_setting_value
    groups_raw = await get_setting_value(db, "guidance", "counselor_groups") or "[]"
    try:
        counselor_groups = json.loads(groups_raw)
    except (json.JSONDecodeError, TypeError):
        counselor_groups = []

    # Load SIS → internal school code map (SIS_A → PES, etc.)
    bmap_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        bmap = json.loads(bmap_raw)
    except (json.JSONDecodeError, TypeError):
        bmap = {}
    # Normalise to upper for matching; values are the internal codes.
    bmap_upper = {k.upper(): v.upper() for k, v in bmap.items()}
    # Reverse (internal → [SIS]) for counselor group matching.
    reverse_map: dict[str, set] = {}
    for sis, internal in bmap_upper.items():
        reverse_map.setdefault(internal, set()).add(sis)

    # Build routing rules identical to auto_queue_new_enrollments so the
    # counselor_name shown in the UI matches the group that would have
    # sent the email.
    from app.modules.roster.guidance_auto import _grade_to_number

    rules = []
    for g in counselor_groups:
        grade_list = g.get("grades", "")
        building = (g.get("building") or "").upper()
        if not building:
            continue
        # Renamed from ``school_codes`` to avoid shadowing the outer
        # variable (the building-scope filter list built at the top of
        # this function). Nothing downstream currently reads it after
        # this loop, but the collision was a footgun waiting to land.
        rule_school_codes = {building} | reverse_map.get(building, set())
        entry = {
            "schools": rule_school_codes,
            "group_name": g.get("name", ""),
            "counselor_name": g.get("counselor_name", ""),
        }
        if grade_list == "*" or not grade_list:
            entry.update({"grades": "*", "grade_nums": set(), "grades_raw": set()})
        else:
            grades_raw = {gr.strip().upper() for gr in str(grade_list).split(",") if gr.strip()}
            grade_nums = set()
            for gr in grades_raw:
                n = _grade_to_number(gr)
                if n is not None:
                    grade_nums.add(n)
            entry.update({"grades": "list", "grade_nums": grade_nums, "grades_raw": grades_raw})
        rules.append(entry)

    def _resolve_counselor(school: str, grade: str) -> str:
        grade_num = _grade_to_number(grade)
        school_upper = (school or "").upper()
        for r in rules:
            if school_upper not in r["schools"]:
                continue
            if r["grades"] == "*":
                return r["counselor_name"] or r["group_name"]
            if grade_num is not None and grade_num in r["grade_nums"]:
                return r["counselor_name"] or r["group_name"]
            if (grade or "").strip().upper() in r["grades_raw"]:
                return r["counselor_name"] or r["group_name"]
        return ""

    # DB status → UI status reverse mapping for rendering.
    def _to_ui_status(db_status: str) -> str:
        if db_status in ("open", "in_progress"):
            return "pending"
        if db_status == "resolved":
            return "scheduled"
        if db_status == "deferred":
            return "cancelled"
        return db_status

    entries = []
    for item in items:
        sinfo = student_map.get(item.student_id, {})
        entries.append({
            "id": item.id,
            "student_id": sinfo.get("sis_id", "") or str(item.student_id),
            "student_name": sinfo.get("name", ""),
            "grade": sinfo.get("grade", ""),
            "school": item.school,
            "school_name": bmap_upper.get((item.school or "").upper(), item.school or ""),
            "counselor_name": _resolve_counselor(item.school or "", sinfo.get("grade", "")),
            "google_email": sinfo.get("email", ""),
            "category": item.category,
            "priority": item.priority,
            "status": _to_ui_status(item.status),
            "membership_code": sinfo.get("membership_code"),
            "membership_label": sinfo.get("membership_label"),
            "membership_enrolled": sinfo.get("membership_enrolled"),
            "notes": item.notes,
            "assigned_to": item.assigned_to,
            "created_by": item.created_by,
            "scheduled_by": item.resolved_by,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        })

    # Explicit FERPA read audit — we enriched entries with student
    # names, grades, SIS IDs, and Google emails from RosterSnapshot.
    # require_action() writes a generic permission-check audit but
    # not a student-data-read audit, so we add one here when the
    # response actually contains student PII.
    if entries:
        await log_action(
            db,
            actor=user.email,
            action="student_data_access:guidance.queue",
            module="roster",
            target=f"{len(entries)} students",
            details=f"school={effective_school or 'ALL'} status={ui_status}",
            ip_address=request.client.host if request and request.client else None,
        )

    await db.commit()  # Persist FERPA audit + require_action audit
    return {
        "total": total,
        "pending_count": pending_count,
        "offset": offset,
        "limit": limit,
        "entries": entries,
        # Per-category open counts for the split-view sub-nav badges.
        # Populated at the same scope (school/status/assigned_to) but
        # NOT filtered by the `category` param — so the badges always
        # show the full breakdown for the current view.
        "counts_by_category": counts_by_cat,
    }


@router.post("/api/roster/guidance/queue")
async def create_queue_item(
    body: GuidanceQueueCreate,
    request: Request,
    user: User = Depends(require_action("roster.guidance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Create a guidance queue item. Building-scoped."""
    student = await repo.get_student_by_id(db, body.student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Scope enforcement
    building_scope = await _get_user_building_scope(db, user.id)
    if building_scope and student.school != building_scope:
        raise HTTPException(status_code=403, detail="Student not in your assigned building")

    try:
        item = await repo.create_queue_item(
            db,
            student_id=body.student_id,
            school=student.school,
            category=body.category,
            priority=body.priority,
            notes=body.notes,
            created_by=user.email,
        )
        await log_action(
            db,
            actor=user.email,
            action="roster.guidance.create",
            module="roster",
            target=f"{student.first_name[0]}. {student.last_name} ({student.sis_id})",
            details=f"category={body.category}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok", "id": item.id}
    except Exception:
        await db.rollback()
        raise


@router.put("/api/roster/guidance/queue/{item_id}")
async def update_queue_item(
    item_id: int,
    body: GuidanceQueueUpdate,
    request: Request,
    user: User = Depends(require_action("roster.guidance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Update a guidance queue item."""
    item = await repo.get_queue_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    # Scope enforcement
    building_scope = await _get_user_building_scope(db, user.id)
    if building_scope and item.school != building_scope:
        raise HTTPException(status_code=403, detail="Queue item not in your assigned building")

    try:
        if body.status:
            item.status = body.status
            if body.status == "resolved":
                from datetime import datetime, timezone
                item.resolved_by = user.email
                item.resolved_at = datetime.now(timezone.utc)
        if body.priority:
            item.priority = body.priority
        if body.notes is not None:
            item.notes = body.notes
        if body.assigned_to is not None:
            item.assigned_to = body.assigned_to

        await log_action(
            db,
            actor=user.email,
            action="roster.guidance.update",
            module="roster",
            target=f"queue_item_{item_id}",
            details=f"status={body.status}, priority={body.priority}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except Exception:
        await db.rollback()
        raise


# ── API: Student account provisioning ────────────────────────────────────

@router.post("/api/roster/accounts/provision")
async def provision_account(
    body: StudentAccountProvision,
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger student Google account provisioning via worker job.
    """
    student = await repo.get_student_by_id(db, body.student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    target_name = f"{student.first_name[0]}. {student.last_name} ({student.sis_id})"
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings
        settings = get_settings()
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        job = await redis.enqueue_job(
            "provision_student_account",
            student.id,
            user.email,
            _job_id=f"provision_student_account:{student.id}:{body.request_id}",
        )

        if not job:
            await log_action(
                db,
                actor=user.email,
                action="roster.accounts.provision.enqueue_skipped",
                module="roster",
                target=target_name,
                details=f"duplicate_job_id, request_id={body.request_id}",
                ip_address=request.client.host if request.client else None,
            )
            await db.commit()
            return {
                "status": "already_queued",
                "student_id": student.id,
                "request_id": body.request_id,
            }

    except Exception as e:
        await log_action(
            db,
            actor=user.email,
            action="roster.accounts.provision.enqueue_failed",
            module="roster",
            target=target_name,
            details=str(e)[:200],
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        raise HTTPException(status_code=503, detail="Worker queue unavailable — provisioning not started")

    try:
        await log_action(
            db,
            actor=user.email,
            action="roster.accounts.provision.enqueue",
            module="roster",
            target=target_name,
            details=f"request_id={body.request_id}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Post-enqueue provision audit commit failed for student {student.id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Provision job was queued, but final logging failed. Student ID: {student.id}",
        )

    return {"status": "enqueued", "student_id": student.id, "request_id": body.request_id}


# ── API: Summary ─────────────────────────────────────────────────────────

@router.get("/api/roster/summary")
async def roster_summary(
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    building_scope = await _get_user_building_scope(db, user.id)
    result = await repo.get_roster_summary(db, school=building_scope)
    await db.commit()  # Persist FERPA audit
    return result


@router.get("/api/roster/config")
async def roster_config(
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    """Return non-secret roster config for the UI."""
    from app.modules.settings.repository import get_setting_value
    return {
        "sis_import_url": await get_setting_value(db, "roster", "sis_import_url") or "",
        "student_email_domain": await get_setting_value(db, "google", "student_domain") or "",
    }


@router.get("/api/roster/schools")
async def list_schools(
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    """Return distinct school codes with names for dynamic dropdowns."""
    from sqlalchemy import text as sa_text
    from app.modules.settings.repository import get_setting_value
    import json as _json
    result = await db.execute(sa_text(
        "SELECT DISTINCT school FROM roster_snapshots WHERE school IS NOT NULL AND school != '' ORDER BY school"
    ))
    codes = [r[0] for r in result.all()]
    names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        names = _json.loads(names_raw)
    except Exception:
        names = {}
    return {"schools": [{"code": c, "name": names.get(c, c)} for c in codes]}


# ── API: Recheck Google ──────────────────────────────────────────────────

@router.post("/api/roster/backfill-google")
async def backfill_google_accounts(
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """
    Start a backfill run — provisions Google accounts for every active
    student that's currently missing one. Useful right after flipping
    ``student_google_writes_enabled`` on so accumulated gaps get closed.

    Body (all optional):
      since_date: 'YYYY-MM-DD' — only students enrolled/created on or
                   after this date.
      schools:    list of building codes to restrict to.
      dry_run:    if true, returns a preview count without touching Google.

    Returns immediately with the enqueued job ID; poll
    ``GET /api/roster/backfill-google/progress`` for live status.
    """
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    since_date = (body.get("since_date") or "").strip() or None
    schools = body.get("schools") or None
    dry_run = bool(body.get("dry_run"))
    trust_name_match = bool(body.get("trust_name_match"))

    # Dry-run runs inline — it's just a DB count, no external calls,
    # returns immediately with the number of candidates. Full runs
    # enqueue the ARQ job which can take minutes.
    if dry_run:
        from sqlalchemy import text
        params = {}
        where = [
            "status = 'active'",
            "(google_status IS NULL OR google_status = '' "
            "OR google_status IN ('missing','not_provisioned'))",
        ]
        if since_date:
            where.append(
                "(COALESCE(enrollment_date, '') >= :since "
                "OR created_at >= (:since || 'T00:00:00+00')::timestamptz)"
            )
            params["since"] = since_date
        if schools:
            where.append("school = ANY(:schools)")
            params["schools"] = schools
        candidates = (await db.execute(text(
            f"SELECT sis_id, first_name, last_name, school, grade, google_status "
            f"FROM roster_snapshots WHERE {' AND '.join(where)} "
            f"ORDER BY school, last_name LIMIT 500"
        ).bindparams(**params))).mappings().all()
        return {
            "status": "dry_run",
            "candidates": len(candidates),
            "preview": [
                {
                    "sis_id": c["sis_id"],
                    "name": f"{c['first_name']} {c['last_name']}",
                    "school": c["school"], "grade": c["grade"],
                    "google_status": c["google_status"],
                }
                for c in candidates[:50]
            ],
        }

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings
        settings = get_settings()
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        j = await redis.enqueue_job(
            "run_student_backfill",
            since_date, user.email, dry_run, schools, trust_name_match,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Worker unavailable: {e}")

    await log_action(
        db, actor=user.email, action="roster.backfill.enqueued",
        module="roster",
        target=f"since={since_date or 'all'} schools={schools or 'all'} trust_names={trust_name_match}",
        details=f"job_id={getattr(j, 'job_id', '?')}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "enqueued", "job_id": getattr(j, "job_id", None), "dry_run": False}


@router.get("/api/roster/backfill-google/progress")
async def backfill_progress(
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Latest backfill progress snapshot (written every 10 students)."""
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "roster", "backfill_progress") or ""
    if not raw:
        return {"state": "idle"}
    import json as _json
    try:
        return _json.loads(raw)
    except Exception:
        return {"state": "idle"}


@router.post("/api/roster/recheck-google")
async def recheck_google(
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Re-verify all students have Google accounts. Creates missing-google change records."""
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.modules.settings.repository import get_setting_value

    domain = await get_setting_value(db, "roster", "student_email_domain") or await get_setting_value(db, "google", "student_domain") or ""
    if not domain:
        raise HTTPException(status_code=400, detail="Student email domain not configured")

    try:
        from app.modules.roster.clever_service import build_expected_email, expected_grad_year

        google = GoogleWorkspaceAdapter(db)
        google_accounts = await google.get_active_accounts(domain=domain)
        google_emails = {a["email"].lower() for a in google_accounts}

        students, _ = await repo.search_students(db, status="active", enrolled_only=False, limit=10000)
        missing = []
        matched = 0
        non_district = 0
        noncompliant = 0
        for s in students:
            sis_email = (s.email or "").lower().strip()
            tags = [t.strip() for t in (s.issue_tags or "").split(",") if t.strip()]

            # ── Google status ──
            if sis_email and not sis_email.endswith(f"@{domain}"):
                s.google_status = "non_district"
                s.email_compliant = False
                if "wrong_domain" not in tags:
                    tags.append("wrong_domain")
                non_district += 1
            elif sis_email and sis_email in google_emails:
                s.google_status = "active"
                if "no_google" in tags:
                    tags.remove("no_google")
                matched += 1
            elif sis_email:
                s.google_status = "missing"
                if "no_google" not in tags:
                    tags.append("no_google")
                missing.append({"sis_id": s.sis_id, "name": f"{s.first_name[0]}. {s.last_name}" if s.first_name else s.last_name, "email": s.email, "school": s.school})
            else:
                s.google_status = "no_email"
                if "no_email" not in tags:
                    tags.append("no_email")

            # ── Email compliance check (district-domain students only) ──
            # Clear old tags first so a student who's now tolerated by
            # the compliance checker (year drift, IDM disambiguator)
            # loses the stale non-compliant tag on this pass.
            for old_tag in ("email_noncompliant", "idm_error", "wrong_format",
                            "year_mismatch", "name_mismatch"):
                if old_tag in tags:
                    tags.remove(old_tag)

            if sis_email and sis_email.endswith(f"@{domain}") and s.first_name and s.last_name and s.grade:
                # Delegate to the canonical checker in clever_service —
                # tolerates ±2yr drift (retained students), IDM-added
                # trailing digits (smithj262@), and hyphen normalization.
                # Previously we did a strict `==` here, which flooded the
                # non-compliant list with false positives on any retained
                # student or disambiguated account.
                from app.modules.roster.clever_service import check_email_compliance
                compliance = check_email_compliance(
                    {
                        "Student_email": sis_email,
                        "Last_name": s.last_name or "",
                        "First_name": s.first_name or "",
                        "Grade": s.grade or "",
                    },
                    ignored_emails=set(),
                    domain=domain,
                )
                if compliance.get("compliant"):
                    s.email_compliant = True
                elif compliance.get("compliant") is False:
                    s.email_compliant = False
                    noncompliant += 1
                    reason = compliance.get("reason") or "wrong_format"
                    # Map the checker's canonical reasons to the tag
                    # vocabulary already in use.
                    tag = {
                        "year_mismatch":   "year_mismatch",
                        "name_mismatch":   "name_mismatch",
                        "format_mismatch": "wrong_format",
                        "insufficient_data": "wrong_format",
                        "wrong_domain":    "wrong_domain",
                    }.get(reason, "wrong_format")
                    if tag not in tags:
                        tags.append(tag)
                # else: compliant is None (insufficient data) — leave as-is

            s.issue_tags = ",".join(tags) if tags else None

        await log_action(
            db, actor=user.email, action="roster.recheck_google",
            module="roster", target=f"{matched} active, {len(missing)} missing, {non_district} non-district, {noncompliant} noncompliant of {len(students)} checked",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"total_checked": len(students), "matched": matched, "missing": len(missing), "non_district": non_district, "noncompliant": noncompliant, "students": missing[:200]}
    except Exception:
        await db.rollback()
        raise


# ── API: Roster changes (diff view) ─────────────────────────────────────

STATUS_CHANGE_TYPES = {"added", "removed", "transferred", "grade_change", "name_change"}


@router.get("/api/roster/changes")
async def roster_changes(
    school: str | None = None,
    change_type: str | None = None,
    days: int = 30,
    today_only: bool = False,
    request: Request = None,
    user: User = Depends(require_action("roster.changes.view")),
    db: AsyncSession = Depends(get_db),
):
    """Diff view of added/removed/modified students with pagination."""
    building_scope = await _get_user_building_scope(db, user.id)
    effective_school = building_scope or school

    if today_only:
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=min(days, 365))

    filter_types = STATUS_CHANGE_TYPES
    if change_type and change_type in STATUS_CHANGE_TYPES:
        filter_types = {change_type}

    changes = await repo.list_roster_changes(
        db,
        change_types=filter_types,
        school_code=effective_school,
        since=cutoff,
    )

    out = []
    for c in changes:
        details = json.loads(c.details) if c.details else {}
        sd = details.get("student_data", {})
        sis_email = sd.get("Student_email", "") or details.get("email", "")
        grade = sd.get("Grade", "") or details.get("grade", {}).get("to", "") if isinstance(details.get("grade"), dict) else details.get("grade", "")

        out.append({
            "id": c.id,
            "student_id": c.student_id,
            "student_name": c.student_name,
            "change_type": c.change_type,
            "school_code": c.school_code,
            "email": sis_email,
            "grade": grade,
            "details": details,
            "reviewed": c.reviewed,
            "google_provisioned": c.google_provisioned,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    await audit_student_view(
        db,
        actor=user.email,
        view_type="changes",
        scope=effective_school or "district",
        ip_address=request.client.host if request and request.client else None,
    )
    await db.commit()
    return out


# ── API: Compliance ─────────────────────────────────────────────────────

@router.get("/api/roster/compliance")
async def roster_compliance(
    school: str | None = None,
    request: Request = None,
    user: User = Depends(require_action("roster.compliance.view")),
    db: AsyncSession = Depends(get_db),
):
    """Per-school email compliance stats from stored change data."""
    building_scope = await _get_user_building_scope(db, user.id)
    effective_school = building_scope or school

    noncompliant = await repo.list_roster_changes(
        db, change_type="email_noncompliant", school_code=effective_school,
    )

    await audit_student_view(
        db, actor=user.email, view_type="compliance",
        scope=effective_school or "district",
        ip_address=request.client.host if request and request.client else None,
    )
    await db.commit()  # Persist FERPA audit
    return {
        "noncompliant_count": len(noncompliant),
        "noncompliant": [
            {
                "student_id": c.student_id,
                "student_name": c.student_name,
                "school_code": c.school_code,
                "details": json.loads(c.details) if c.details else {},
            }
            for c in noncompliant
        ],
    }


@router.get("/api/roster/compliance-export")
async def compliance_export(
    request: Request,
    user: User = Depends(require_action("roster.compliance.export")),
    db: AsyncSession = Depends(get_db),
):
    """CSV export of email compliance violations. Admin-only, audited."""
    from app.modules.roster.service import export_compliance_csv

    csv_data = await export_compliance_csv(
        db,
        actor=user.email,
        ip_address=request.client.host if request.client else None,
    )
    if not csv_data:
        raise HTTPException(status_code=404, detail="No compliance issues found")

    await db.commit()
    filename = f"email_compliance_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── API: Missing Google accounts ────────────────────────────────────────

@router.get("/api/roster/missing-google")
async def missing_google(
    school: str | None = None,
    request: Request = None,
    user: User = Depends(require_action("roster.compliance.view")),
    db: AsyncSession = Depends(get_db),
):
    """Students added but not yet Google-provisioned."""
    building_scope = await _get_user_building_scope(db, user.id)
    effective_school = building_scope or school

    changes = await repo.get_unprovisioned_added(db, school_code=effective_school)

    await db.commit()  # Persist FERPA audit
    return {
        "count": len(changes),
        "students": [
            {
                "id": c.id,
                "student_id": c.student_id,
                "student_name": c.student_name,
                "school_code": c.school_code,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in changes
        ],
    }


@router.get("/api/roster/missing-email-export")
async def missing_email_export(
    request: Request,
    user: User = Depends(require_action("roster.compliance.export")),
    db: AsyncSession = Depends(get_db),
):
    """CSV export of students with missing emails. Admin-only, audited."""
    from app.modules.roster.service import export_missing_emails_csv

    csv_data = await export_missing_emails_csv(
        db,
        actor=user.email,
        ip_address=request.client.host if request.client else None,
    )
    if not csv_data:
        raise HTTPException(status_code=404, detail="No students with missing emails")

    await db.commit()
    filename = f"missing_emails_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── API: Google change log ──────────────────────────────────────────────

@router.get("/api/roster/google-changelog")
async def google_changelog(
    days: int = 60,
    action: str | None = None,
    request: Request = None,
    user: User = Depends(require_action("roster.google.view")),
    db: AsyncSession = Depends(get_db),
):
    """History of all Google Workspace mutations made by Command Nexus."""
    changes = await repo.list_google_changes(db, days=days, action=action)

    await audit_student_view(
        db,
        actor=user.email,
        view_type="google_changelog",
        scope="district",
        ip_address=request.client.host if request and request.client else None,
    )
    await db.commit()

    return [
        {
            "id": c.id,
            "changed_at": c.changed_at.isoformat(),
            "actor": c.actor,
            "action": c.action,
            "target_email": c.target_email,
            "student_id": c.student_id,
            "before_state": json.loads(c.before_state) if c.before_state else {},
            "after_state": json.loads(c.after_state) if c.after_state else {},
            "reverted": c.reverted,
            "reverted_at": c.reverted_at.isoformat() if c.reverted_at else None,
            "reverted_by": c.reverted_by,
        }
        for c in changes
    ]


# ── API: Google revert ──────────────────────────────────────────────────

@router.post("/api/roster/google-revert")
async def google_revert(
    body: RevertGoogleRequest,
    request: Request,
    user: User = Depends(require_action("roster.google.manage")),
    db: AsyncSession = Depends(get_db),
):
    """
    Revert all Google Workspace changes made after a given datetime.
    Processes in reverse chronological order (newest first).
    """
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    try:
        revert_to = datetime.fromisoformat(body.revert_to)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid datetime -- use ISO format e.g. 2026-03-20T09:00:00",
        )

    changes = await repo.get_unrevert_changes_after(db, revert_to)
    if not changes:
        return {"reverted": 0, "skipped": 0, "errors": [], "message": "Nothing to revert"}

    google = GoogleWorkspaceAdapter(db)
    reverted = 0
    skipped = 0
    errors = []
    now = datetime.now(timezone.utc)

    for c in changes:
        before = json.loads(c.before_state) if c.before_state else {}
        try:
            if c.action == "create_account":
                await google.suspend_account(c.target_email)
            elif c.action == "reactivate_account":
                await google.suspend_account(c.target_email)
            elif c.action == "rename_account":
                old_email = before.get("email")
                if not old_email:
                    skipped += 1
                    continue
                await google.rename_account(c.target_email, old_email)
            elif c.action == "set_student_id":
                old_sid = before.get("student_id")
                if old_sid:
                    await google.set_student_id(c.target_email, old_sid)
                else:
                    await google.clear_student_id(c.target_email)
            elif c.action == "suspend_account":
                await google.reactivate_account(c.target_email)
            else:
                skipped += 1
                continue

            c.reverted = True
            c.reverted_at = now
            c.reverted_by = user.email
            reverted += 1
        except Exception as e:
            errors.append(f"{c.action} {c.target_email}: {str(e)[:100]}")
            logger.error(f"Revert failed -- {c.action} {c.target_email}: {e}")

    await log_action(
        db,
        actor=user.email,
        action="roster.google.revert",
        module="roster",
        target=f"revert_to={body.revert_to}",
        details=f"reverted={reverted}, skipped={skipped}, errors={len(errors)}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"reverted": reverted, "skipped": skipped, "errors": errors}


# ── API: Google duplicate accounts ──────────────────────────────────────

@router.get("/api/roster/google-duplicates")
async def google_duplicates(
    request: Request = None,
    user: User = Depends(require_action("roster.google.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Find Google accounts where Clever IDM appended a numeric suffix instead of
    using the correct disambiguation format (first 2 letters of first name).
    """
    import re
    from app.modules.roster.clever_service import build_expected_email, grad_year_to_grade_label
    from app.modules.settings.repository import get_setting_value
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    domain = await get_setting_value(db, "roster", "student_email_domain") or ""

    google = GoogleWorkspaceAdapter(db)
    try:
        accounts = await google.get_active_accounts(domain)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not fetch Google accounts: {e}")

    email_map = {a["email"].lower(): a for a in accounts}
    suffix_pat = re.compile(rf"^([a-z]+)(\d{{2}})(\d+)@{re.escape(domain)}$")

    dupe_records = []
    for email, acct in email_map.items():
        m = suffix_pat.match(email)
        if not m:
            continue
        prefix, grad_str, numeric_suffix = m.group(1), m.group(2), m.group(3)
        base_email = f"{prefix}{grad_str}@{domain}"
        if base_email not in email_map:
            continue  # no primary account -- not a Clever IDM collision

        given = acct.get("given_name", "").strip()
        family = acct.get("family_name", "").strip()
        grad_year = 2000 + int(grad_str)

        correct_email = (
            build_expected_email(family, given, grad_year, domain=domain, disambiguate=True)
            if given and family and len(given) >= 2 else None
        )
        conflict = bool(correct_email and correct_email.lower() in email_map)
        grade_label = grad_year_to_grade_label(grad_year)

        dupe_records.append({
            "duplicate_email": email,
            "student_name": acct.get("name", ""),
            "correct_email": correct_email,
            "correct_email_conflict": conflict,
            "numeric_suffix": numeric_suffix,
            "grade": grade_label,
        })

    dupe_records.sort(key=lambda x: x["duplicate_email"])
    return {"count": len(dupe_records), "duplicates": dupe_records}


# ── API: Resolve duplicate ──────────────────────────────────────────────

@router.post("/api/roster/resolve-duplicate")
async def resolve_duplicate(
    body: ResolveDuplicateRequest,
    request: Request,
    user: User = Depends(require_action("roster.google.manage")),
    db: AsyncSession = Depends(get_db),
):
    """
    Rename a Clever IDM-created duplicate account to the correct
    disambiguated format. The original account is untouched.
    """
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    google = GoogleWorkspaceAdapter(db)

    if not await google.check_email_exists(body.duplicate_email):
        raise HTTPException(status_code=404, detail=f"Account not found: {body.duplicate_email}")
    if await google.check_email_exists(body.correct_email):
        raise HTTPException(status_code=409, detail=f"Target email already exists: {body.correct_email}")

    result = await google.rename_account(body.duplicate_email, body.correct_email)
    if not result.success:
        raise HTTPException(status_code=500, detail="Rename failed -- check server logs")

    await repo.create_google_change(
        db,
        actor=user.email,
        action="rename_account",
        target_email=body.correct_email,
        before_state=json.dumps({"email": body.duplicate_email}),
        after_state=json.dumps({"email": body.correct_email}),
    )

    await log_action(
        db,
        actor=user.email,
        action="roster.google.resolve_duplicate",
        module="roster",
        target=f"{body.duplicate_email} -> {body.correct_email}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "old_email": body.duplicate_email, "new_email": body.correct_email}


# ── API: Student actions ────────────────────────────────────────────────

@router.post("/api/roster/confirm-student-id")
async def confirm_student_id(
    body: ConfirmStudentIdRequest,
    request: Request,
    user: User = Depends(require_action("roster.google.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Manually confirm and backfill a student ID into a Google account."""
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    google = GoogleWorkspaceAdapter(db)
    try:
        result = await google.set_student_id(body.google_email, body.student_id)
        if result.success:
            await repo.create_google_change(
                db,
                actor=user.email,
                action="set_student_id",
                target_email=body.google_email,
                student_id=body.student_id,
                before_state=json.dumps(result.before_state or {}),
                after_state=json.dumps({"student_id": body.student_id}),
            )
        await log_action(
            db,
            actor=user.email,
            action="roster.google.confirm_student_id",
            module="roster",
            target=f"SID {body.student_id} -> {body.google_email}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.post("/api/roster/ignore-email")
async def ignore_email(
    body: IgnoreEmailRequest,
    request: Request,
    user: User = Depends(require_action("roster.compliance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Add an email to the compliance whitelist."""
    await repo.create_or_restore_email_ignore(
        db, email=body.email, reason=body.reason, ignored_by=user.email,
    )
    await log_action(
        db,
        actor=user.email,
        action="roster.compliance.ignore_email",
        module="roster",
        target=body.email,
        details=body.reason or None,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "email": body.email}


@router.delete("/api/roster/ignore-email/{email}")
async def restore_email(
    email: str,
    request: Request,
    user: User = Depends(require_action("roster.compliance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Remove an email from the compliance whitelist."""
    record = await repo.get_email_ignore(db, email)
    if not record:
        raise HTTPException(status_code=404, detail="Email not found in ignore list")

    record.restored_at = datetime.now(timezone.utc)
    await log_action(
        db,
        actor=user.email,
        action="roster.compliance.restore_email",
        module="roster",
        target=email,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "email": email}


# ── API: Guidance queue (enhanced) ──────────────────────────────────────

@router.post("/api/roster/guidance/queue/{item_id}/schedule")
async def schedule_guidance_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("roster.guidance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Mark a guidance queue item as scheduled."""
    item = await repo.get_queue_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    building_scope = await _get_user_building_scope(db, user.id)
    if building_scope and item.school != building_scope:
        raise HTTPException(status_code=403, detail="Queue item not in your assigned building")

    item.status = "resolved"
    item.resolved_by = user.email
    item.resolved_at = datetime.now(timezone.utc)

    await log_action(
        db,
        actor=user.email,
        action="roster.guidance.schedule",
        module="roster",
        target=f"queue_item_{item_id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/roster/guidance/queue/{item_id}/cancel")
async def cancel_guidance_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("roster.guidance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a guidance queue item."""
    item = await repo.get_queue_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    building_scope = await _get_user_building_scope(db, user.id)
    if building_scope and item.school != building_scope:
        raise HTTPException(status_code=403, detail="Queue item not in your assigned building")

    item.status = "deferred"
    item.resolved_by = user.email
    item.resolved_at = datetime.now(timezone.utc)

    await log_action(
        db,
        actor=user.email,
        action="roster.guidance.cancel",
        module="roster",
        target=f"queue_item_{item_id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/roster/guidance/queue/{item_id}/no-schedule-needed")
async def no_schedule_needed(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("roster.guidance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Move a queue item out of pending into a "no schedule needed"
    disposition. For A/E enrollees showing up as pending — counselor
    confirms they don't need a the district schedule.

    Reuses the existing `deferred` DB status so we don't need a
    migration; distinguishes with an audit action + notes prefix so
    reports can tell "cancelled" from "no schedule needed" if we
    ever want per-disposition analytics."""
    item = await repo.get_queue_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    building_scope = await _get_user_building_scope(db, user.id)
    if building_scope and item.school != building_scope:
        raise HTTPException(status_code=403, detail="Queue item not in your assigned building")

    item.status = "deferred"
    item.resolved_by = user.email
    item.resolved_at = datetime.now(timezone.utc)
    prefix = "[no-schedule-needed] "
    if item.notes and prefix not in item.notes:
        item.notes = prefix + item.notes
    elif not item.notes:
        item.notes = prefix.strip()

    await log_action(
        db,
        actor=user.email,
        action="roster.guidance.no_schedule_needed",
        module="roster",
        target=f"queue_item_{item_id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/roster/guidance/queue/auto-resolve")
async def auto_resolve_guidance(
    request: Request,
    user: User = Depends(require_action("roster.guidance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """
    Bulk auto-resolve: mark open guidance items as resolved if the student
    now has teacher assignments (sections found).
    """
    from sqlalchemy import select as sa_select
    from app.modules.roster.models import GuidanceQueue, StudentTeacher

    open_result = await db.execute(
        sa_select(GuidanceQueue).where(GuidanceQueue.status == "open")
    )
    open_items = open_result.scalars().all()
    if not open_items:
        return {"resolved": 0}

    # Get student IDs that now have teacher assignments
    student_ids = [item.student_id for item in open_items]
    has_sections = await db.execute(
        sa_select(StudentTeacher.student_id)
        .where(StudentTeacher.student_id.in_(student_ids))
        .distinct()
    )
    resolved_ids = {r[0] for r in has_sections.all()}

    resolved_count = 0
    for item in open_items:
        if item.student_id in resolved_ids:
            item.status = "resolved"
            item.resolved_by = "system"
            item.resolved_at = datetime.now(timezone.utc)
            resolved_count += 1

    if resolved_count:
        await log_action(
            db,
            actor=user.email,
            action="roster.guidance.auto_resolve",
            module="roster",
            target=f"{resolved_count} items",
            ip_address=request.client.host if request.client else None,
        )
    await db.commit()
    return {"resolved": resolved_count}


@router.post("/api/roster/guidance/queue/run-auto-scan")
async def run_guidance_auto_scan(
    request: Request,
    user: User = Depends(require_action("roster.guidance.manage")),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually run the guidance auto-queue scan.

    Queue insertion is always on — this endpoint lets admins drain
    the backlog of new enrollees and withdrawals on demand. Uses the
    ``guidance.auto_queue_recent_days`` window (default 3), so a
    single click queues only recently-added students, not the full
    ~860-row backlog of resident-only students who attend non-district
    schools.

    Counselor email notifications triggered by this scan are still
    gated by ``guidance.notify_enabled`` — if it's off, the queue
    fills silently and no emails go out.

    Runs both enrollment and withdrawal scans in sequence. Returns a
    combined result with counts + counselor notifications sent.
    """
    from app.modules.roster.guidance_auto import (
        auto_queue_new_enrollments,
        auto_queue_recent_withdrawals,
        auto_resolve_with_sections,
    )

    enroll_result = await auto_queue_new_enrollments(db, force=True)
    withdraw_result = await auto_queue_recent_withdrawals(db, force=True)
    resolved_count = await auto_resolve_with_sections(db)
    await db.commit()

    await log_action(
        db,
        actor=user.email,
        action="roster.guidance.run_auto_scan",
        module="roster",
        target=(
            f"enrollments={enroll_result.get('queued', 0)} "
            f"withdrawals={withdraw_result.get('queued', 0)} "
            f"auto_resolved={resolved_count}"
        ),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return {
        "enrollments": enroll_result,
        "withdrawals": withdraw_result,
        "auto_resolved": resolved_count,
    }


# ── API: Guidance Notes ──────────────────────────────────────────────────

@router.get("/api/roster/guidance/queue/{item_id}/notes")
async def get_guidance_notes(
    item_id: int,
    user: User = Depends(require_action("roster.guidance.view")),
    db: AsyncSession = Depends(get_db),
):
    """Get all notes for a guidance queue item."""
    from sqlalchemy import text
    result = await db.execute(text(
        "SELECT id, note, author, created_at FROM guidance_notes WHERE queue_id = :qid ORDER BY created_at DESC"
    ).bindparams(qid=item_id))
    return {"notes": [
        {"id": r[0], "note": r[1], "author": r[2], "date": r[3].isoformat() if r[3] else None}
        for r in result.all()
    ]}


@router.post("/api/roster/guidance/queue/{item_id}/notes")
async def add_guidance_note(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("roster.guidance.view")),
    db: AsyncSession = Depends(get_db),
):
    """Add a note to a guidance queue item. Principals and guidance can add notes."""
    body = await request.json()
    note = (body.get("note") or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="Note text is required")
    from sqlalchemy import text
    await db.execute(text(
        "INSERT INTO guidance_notes (queue_id, note, author) VALUES (:qid, :note, :author)"
    ).bindparams(qid=item_id, note=note, author=user.email))
    await db.commit()
    return {"status": "ok"}


# ── API: NutriKids export ───────────────────────────────────────────────

@router.get("/api/roster/nutrikids-export")
async def nutrikids_export(
    days: int = 30,
    full: bool = False,
    request: Request = None,
    user: User = Depends(require_action("roster.students.export")),
    db: AsyncSession = Depends(get_db),
):
    """
    NutriKids-format CSV export.

    Default: recently added students within ``days``.
    ``full=1``: every active student — start-of-year initial upload.
    """
    from app.modules.roster.service import export_nutrikids_csv

    csv_data = await export_nutrikids_csv(
        db,
        days=days,
        full=full,
        actor=user.email,
        ip_address=request.client.host if request and request.client else None,
    )
    if not csv_data:
        raise HTTPException(
            status_code=404,
            detail="No active students found" if full else "No new students in this time period",
        )

    await db.commit()
    tag = "full" if full else "changes"
    filename = f"nutrikids_{tag}_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── API: Today's Roster Changes ─────────────────────────────────────────

@router.get("/api/roster/todays-changes")
async def todays_changes(
    days: int = Query(default=1, le=30),
    school: str | None = None,
    request: Request = None,
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Today's roster changes for dashboard panel.
    Building-scoped for principals, all buildings for admins.
    Full names (T0.4 only applies to outbound emails, not in-app).
    """
    building_scope = await _get_user_building_scope(db, user.id)
    if building_scope == "__no_scope__":
        raise HTTPException(status_code=403, detail="No roster scope")

    effective_school = school or building_scope  # None = all buildings (admin)

    since = datetime.now(timezone.utc) - timedelta(days=days)

    change_types = {"added", "removed", "transferred", "grade_change", "name_change", "account_review"}
    changes = await repo.list_roster_changes(
        db,
        change_types=change_types,
        school_code=effective_school or "ALL",
        since=since,
        limit=200,
    )

    result = {
        "filter_days": days,
        "school": effective_school,
        "changes": [],
        "summary": {},
    }

    for c in changes:
        details = json.loads(c.details) if c.details else {}
        result["changes"].append({
            "id": c.id,
            "student_id": c.student_id,
            "student_name": c.student_name,
            "change_type": c.change_type,
            "school_code": c.school_code,
            "details": details,
            "reviewed": c.reviewed,
            "google_provisioned": c.google_provisioned,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })
        ct = c.change_type
        result["summary"][ct] = result["summary"].get(ct, 0) + 1

    # Explicit FERPA read audit — the response carries student names
    # and IDs. require_action() wrote a generic permission audit but
    # not a student-data-read audit, so we add one here when the
    # response actually contains student rows. The subsequent commit
    # also persists the require_action row, which was previously lost
    # because this endpoint never committed.
    if result["changes"]:
        await log_action(
            db,
            actor=user.email,
            action="student_data_access:roster.todays_changes",
            module="roster",
            target=f"{len(result['changes'])} changes",
            details=f"school={effective_school or 'ALL'} days={days}",
            ip_address=request.client.host if request and request.client else None,
        )
    await db.commit()

    return result


# ── API: Resolve Account Review ──────────────────────────────────────────

@router.post("/api/roster/resolve-account-review/{change_id}")
async def resolve_account_review(
    change_id: int,
    request: Request = None,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """
    IT confirms an account_review match is correct:
    - Reactivates the account if suspended
    - Writes the SID
    - Moves to correct OU
    - Marks the RosterChange as reviewed
    - Audit logged
    """
    from app.modules.roster.provision import resolve_account_review as _resolve

    result = await _resolve(db, change_id, actor=user.email)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("detail", "Resolution failed"))

    await db.commit()
    return result


# ── Deprovision-exemption toggle ────────────────────────────────────
#
# Applies to kids where the sweep would otherwise archive them —
# non-district email + no student_teachers rows. Alt-program enrollments
# (credit recovery, vocational partnership) don't show up in the SIS
# Enrollments feed, so the operator has to tag them by hand.

@router.post("/api/roster/students/{sis_id}/deprov-exemption")
async def add_deprov_exemption(
    sis_id: str,
    payload: dict,
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Tag a student as exempt from the deprovision sweep. Idempotent —
    re-adding updates the reason/notes and refreshes added_by."""
    from sqlalchemy import text as _t
    reason = (payload.get("reason") or "").strip() or None
    notes = (payload.get("notes") or "").strip() or None
    import json as _json_local
    await db.execute(_t("""
        INSERT INTO roster_deprov_exemptions (sis_id, reason, added_by, notes)
        VALUES (:sid, :reason, :actor, :notes)
        ON CONFLICT (sis_id) DO UPDATE
        SET reason = EXCLUDED.reason,
            added_by = EXCLUDED.added_by,
            notes = EXCLUDED.notes,
            added_at = NOW()
    """).bindparams(sid=sis_id, reason=reason, actor=user.email, notes=notes))
    await log_action(
        db, actor=user.email, action="roster.deprov_exemption.added",
        module="roster", target=sis_id,
        details=_json_local.dumps({"reason": reason, "notes": notes}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"ok": True, "sis_id": sis_id, "exempted": True}


@router.delete("/api/roster/students/{sis_id}/deprov-exemption")
async def remove_deprov_exemption(
    sis_id: str,
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Clear a student's deprovision exemption — sweep will treat them
    as a normal candidate again."""
    from sqlalchemy import text as _t
    r = await db.execute(_t(
        "DELETE FROM roster_deprov_exemptions WHERE sis_id = :sid"
    ).bindparams(sid=sis_id))
    if r.rowcount:
        await log_action(
            db, actor=user.email, action="roster.deprov_exemption.removed",
            module="roster", target=sis_id,
            ip_address=request.client.host if request.client else None,
        )
    await db.commit()
    return {"ok": True, "sis_id": sis_id, "exempted": False,
            "was_present": bool(r.rowcount)}


@router.get("/api/roster/deprov-exemptions")
async def list_deprov_exemptions(
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """List all current exemptions with student names for the admin view."""
    from sqlalchemy import text as _t
    rows = (await db.execute(_t("""
        SELECT e.sis_id, e.reason, e.added_by,
               to_char(e.added_at,'YYYY-MM-DD HH24:MI') AS added_at,
               e.notes,
               r.first_name, r.last_name, r.school, r.grade, r.email, r.status
        FROM roster_deprov_exemptions e
        LEFT JOIN roster_snapshots r ON r.sis_id = e.sis_id
        ORDER BY e.added_at DESC
    """))).mappings().all()
    return {"count": len(rows), "exemptions": [dict(r) for r in rows]}


# ── Student membership status (a201) ────────────────────────────────

@router.get("/settings/student-membership", response_class=HTMLResponse)
async def student_membership_settings_page(
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Settings page for editing the membership code → {label, enrolled}
    map. Reached via /settings hub. Read-only display of what codes
    the last import saw so operators can spot new codes needing labels."""
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    # Freshness card — when was the last import, and what's the distribution?
    stats_row = (await db.execute(text("""
        SELECT MAX(imported_at) AS last_imported,
               COUNT(*)          AS total_students
        FROM student_membership_status
    """))).mappings().first()
    code_counts = (await db.execute(text("""
        SELECT code, COUNT(*) AS n
        FROM student_membership_status
        GROUP BY code
        ORDER BY n DESC
    """))).mappings().all()
    return templates.TemplateResponse("settings_student_membership.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "last_imported": stats_row["last_imported"] if stats_row else None,
        "total_students": (stats_row["total_students"] if stats_row else 0) or 0,
        "code_counts": [dict(r) for r in code_counts],
    })

# SIS-authoritative enrollment code (A / R / CTC / etc.) imported from
# the MetaSolutions "Memberships All Yrs All Info" morning report.
# Complements roster_snapshots.status (which is just "in Clever feed?").
# See app/modules/roster/membership.py for parser + effective-enrolled
# helper; template chip on the student profile modal drives the
# per-student "Attends our classes" override.

@router.post("/api/roster/membership/upload")
async def upload_membership_csv(
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Manual CSV upload. Accepts multipart/form-data with a `file`
    field containing the "Memberships All Yrs All Info" export.

    Used until the email poller integration lands; also handy for
    catching up after a missed morning run."""
    from app.modules.roster.membership import import_csv
    form = await request.form()
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        raise HTTPException(status_code=400, detail="Missing file upload")
    data = await up.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    result = await import_csv(db, data, source_subject=f"manual upload by {user.email}")
    await log_action(
        db, actor=user.email, action="roster.membership.imported",
        module="roster", target="student_membership_status",
        details=json.dumps({
            "source": "manual upload",
            "total_rows": result.total_rows,
            "unique_students": result.unique_students,
            "upserted": result.upserted,
            "codes_seen": dict(result.codes_seen or {}),
            "unknown_codes": result.unknown_codes,
        }),
    )
    await db.commit()
    return {
        "ok": True,
        "total_rows": result.total_rows,
        "unique_students": result.unique_students,
        "upserted": result.upserted,
        "codes_seen": dict(result.codes_seen or {}),
        "unknown_codes": result.unknown_codes,
    }


@router.get("/api/roster/membership/stats")
async def get_membership_stats(
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    """Freshness + per-code counts for the settings panel. Same data
    that the standalone /settings/student-membership page renders at
    load; exposed as JSON so the inline panel can fetch it lazily."""
    stats = (await db.execute(_text("""
        SELECT MAX(imported_at) AS last_imported,
               COUNT(*) AS total_students
        FROM student_membership_status
    """))).mappings().first()
    code_counts = (await db.execute(_text("""
        SELECT code, COUNT(*) AS n
        FROM student_membership_status
        GROUP BY code
        ORDER BY n DESC
    """))).mappings().all()
    return {
        "last_imported": stats["last_imported"].isoformat() if stats and stats["last_imported"] else None,
        "total_students": (stats["total_students"] if stats else 0) or 0,
        "code_counts": [dict(r) for r in code_counts],
    }


@router.get("/api/roster/membership/code-map")
async def get_membership_code_map(
    user: User = Depends(require_action("roster.view")),
    db: AsyncSession = Depends(get_db),
):
    """Current code → {label, enrolled} mapping. Falls back to defaults
    if the setting hasn't been customized yet."""
    from app.modules.roster.membership import get_code_map
    return {"map": await get_code_map(db)}


@router.put("/api/roster/membership/code-map")
async def put_membership_code_map(
    payload: dict,
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Replace the code map. Payload: {map: {"A": {"label": "...", "enrolled": true}, ...}}."""
    from app.modules.settings.repository import upsert_integration_setting
    m = payload.get("map") if isinstance(payload, dict) else None
    if not isinstance(m, dict):
        raise HTTPException(status_code=400, detail="payload.map must be a dict")
    # Light validation — every entry needs label + enrolled bool.
    # auto_deprovision defaults False when missing (backwards-compat).
    for code, entry in m.items():
        if not isinstance(entry, dict) or "label" not in entry or "enrolled" not in entry:
            raise HTTPException(status_code=400,
                detail=f"code {code!r} entry must have label + enrolled")
        entry["enrolled"] = bool(entry["enrolled"])
        entry["auto_deprovision"] = bool(entry.get("auto_deprovision", False))
        entry["label"] = str(entry["label"])[:120]
    await upsert_integration_setting(
        db, integration="roster", key="membership_code_map",
        value=json.dumps(m), updated_by=user.email,
    )
    await log_action(
        db, actor=user.email, action="roster.membership.code_map_updated",
        module="roster", target="membership_code_map",
        details=json.dumps({"codes": sorted(m.keys()), "count": len(m)}),
    )
    await db.commit()
    return {"ok": True, "count": len(m)}


@router.post("/api/roster/students/{sis_id}/attends-our-classes")
async def add_attends_our_classes(
    sis_id: str,
    payload: dict,
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Mark a student as attending the district classes despite an A/E-ish
    membership code. Same shape/pattern as `add_deprov_exemption`."""
    from sqlalchemy import text as _t
    reason = (payload.get("reason") or "").strip() or None
    notes = (payload.get("notes") or "").strip() or None
    await db.execute(_t("""
        INSERT INTO student_attends_our_classes (sis_id, reason, added_by, notes)
        VALUES (:sid, :reason, :actor, :notes)
        ON CONFLICT (sis_id) DO UPDATE
        SET reason = EXCLUDED.reason,
            added_by = EXCLUDED.added_by,
            notes = EXCLUDED.notes,
            added_at = NOW()
    """).bindparams(sid=sis_id, reason=reason, actor=user.email, notes=notes))
    await log_action(
        db, actor=user.email, action="roster.attends_our_classes.added",
        module="roster", target=sis_id,
        details=json.dumps({"reason": reason, "notes": notes}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"ok": True, "sis_id": sis_id, "attends_our_classes": True}


@router.delete("/api/roster/students/{sis_id}/attends-our-classes")
async def remove_attends_our_classes(
    sis_id: str,
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Clear the attends-our-classes override — student falls back to
    the membership-code default (usually not enrolled for A/E codes)."""
    from sqlalchemy import text as _t
    r = await db.execute(_t(
        "DELETE FROM student_attends_our_classes WHERE sis_id = :sid"
    ).bindparams(sid=sis_id))
    if r.rowcount:
        await log_action(
            db, actor=user.email, action="roster.attends_our_classes.removed",
            module="roster", target=sis_id,
            ip_address=request.client.host if request.client else None,
        )
    await db.commit()
    return {"ok": True, "sis_id": sis_id, "attends_our_classes": False,
            "was_present": bool(r.rowcount)}


@router.post("/api/roster/students/{sis_id}/reset-google-password")
async def reset_student_google_password(
    sis_id: str,
    request: Request,
    user: User = Depends(require_action("roster.accounts.provision")),
    db: AsyncSession = Depends(get_db),
):
    """Reset the student's Google Workspace password to the district's standard
    default: `0000` + last four of SID. Force-change-at-next-login is
    OFF for students — this default IS the password they keep using
    (device unlocks, Chromebook sign-ins, etc.). If we forced a change
    they'd have to invent something and then forget it a week later.
    Returns the temp password so the tech can relay it — deterministic,
    so if they miss the modal they can regenerate it in their head.

    Same permission gate as backfill/provision (roster.accounts.provision).
    Building-scoped users can only reset students in their assigned school.

    Logs to audit_logs and api_log without the password value."""
    student = await repo.get_student_by_sis_id(db, sis_id)
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    building_scope = await _get_user_building_scope(db, user.id)
    if building_scope and student.school != building_scope:
        raise HTTPException(status_code=403, detail="Student not in your assigned building")

    if not student.email:
        raise HTTPException(status_code=400, detail="Student has no email on file")

    # the district default: 0000 + last 4 of SIS ID.
    # Every district SID is at least 7 digits so slicing [-4:] is safe.
    last4 = (sis_id or "").strip()[-4:]
    if len(last4) < 4:
        # Defensive — a malformed SID shouldn't crash the reset path.
        # Pad with zeros so we always produce an 8-char password.
        last4 = last4.rjust(4, "0")
    temp_pw = f"0000{last4}"

    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.integrations.api_log import log_api_command as _log_api
    gws = GoogleWorkspaceAdapter(db)
    # Students keep the 0000+last4 default — do NOT force change at
    # next login. Kids would just re-invent-and-forget.
    result = await gws.set_password(student.email, temp_pw, force_change=False)

    _log_api(
        system="google", action="set_password", target=student.email,
        response_status="ok" if result.success else "error",
        caller=f"roster.reset_password:{user.email}",
    )

    if not result.success:
        await log_action(
            db, actor=user.email, action="student.password.reset.failed",
            module="roster", target=student.email,
            details=f"sis_id={sis_id}; Google error: {(result.error or '')[:150]}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"Google password reset failed: {result.error}",
        )

    await log_action(
        db, actor=user.email, action="student.password.reset",
        module="roster", target=student.email,
        details=f"sis_id={sis_id}; force_change_at_next_login=false; temp_pw NOT logged",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return {
        "email": student.email,
        "temp_password": temp_pw,
        "force_change_at_next_login": False,
    }
