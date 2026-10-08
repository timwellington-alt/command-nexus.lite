"""
Roster service — import pipeline, export, and student account provisioning.

Import runs in the worker (roster_job.py), not inline in a request handler.
Export enforces T0.4 field allowlists.

Non-negotiable per T0.4/T0.5:
- No student PII on public endpoints
- Exports admin-only, audited, field-minimized
- Notifications use first-initial+last for staff, full name for guidance
- No student photos (T0.6 blocked)
"""

import csv
import io
import json
import logging
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.roster import repository as repo
from app.audit.service import log_action
from app.audit.student import audit_student_export

logger = logging.getLogger(__name__)


# ── Import pipeline ──────────────────────────────────────────────────────

async def run_import(db: AsyncSession, import_id: int, data: list[dict]) -> dict:
    """
    Process a roster import batch. Called from the worker job.

    Each record must have at least: sis_id, first_name, last_name, school.
    Returns {"added": N, "updated": N, "unchanged": N, "errors": N}.
    """
    imp = await repo.get_import(db, import_id)
    if not imp:
        raise ValueError(f"Import record {import_id} not found")

    imp.status = "running"
    imp.total_records = len(data)

    added = updated = unchanged = errors = 0
    error_lines = []
    sis_ids_seen = set()
    schools_seen = set()

    for i, record in enumerate(data):
        try:
            sis_id = record.get("sis_id", "").strip()
            if not sis_id:
                errors += 1
                error_lines.append(f"Row {i+1}: missing sis_id")
                continue

            sis_ids_seen.add(sis_id)
            school = record.get("school", "").strip()
            if school:
                schools_seen.add(school)

            student, action = await repo.upsert_student(
                db,
                sis_id=sis_id,
                import_id=import_id,
                first_name=record.get("first_name", "").strip(),
                last_name=record.get("last_name", "").strip(),
                middle_name=record.get("middle_name", "").strip() or None,
                dob=record.get("dob"),
                email=record.get("email"),
                school=school,
                grade=record.get("grade"),
                status=record.get("status", "active"),
                enrollment_date=record.get("enrollment_date"),
                withdrawal_date=record.get("withdrawal_date"),
                parent_guardian=record.get("parent_guardian"),
                phone=record.get("phone"),
                address=record.get("address"),
                google_status=record.get("google_status"),
                email_compliant=record.get("email_compliant"),
                # issue_tags is stored as a plain comma-joined string
                # (matches the format read + written by recheck-google).
                # Historical code json.dumps()'d it here, producing
                # `"wrong_format"` (with quotes) — that caused string
                # comparisons and .split(",") lookups to miss entries
                # they should have matched. Normalize on both shapes so
                # existing rows don't break parsers, but always write
                # the flat comma form going forward.
                issue_tags=(
                    ",".join(record["issue_tags"]) if isinstance(record.get("issue_tags"), list)
                    else (record.get("issue_tags") or None)
                ),
            )

            # Teacher assignments (if present in import data)
            assignments = record.get("teacher_assignments", [])
            if assignments:
                await repo.replace_teacher_assignments(db, student.id, school, assignments)

            if action == "added":
                added += 1
            elif action == "updated":
                updated += 1
            else:
                unchanged += 1

        except Exception as e:
            errors += 1
            error_lines.append(f"Row {i+1} (sis_id={record.get('sis_id', '?')}): {str(e)[:100]}")

    # ── Withdrawal detection is NO LONGER done here ─────────────────
    #
    # Per-school, per-CSV mark_students_inactive fires false withdrawals
    # every time we process more than one Students CSV in a run (backlog,
    # multi-day catch-up). Withdrawal is now computed ONCE at the end of
    # poll_clever_imports, district-wide, against the union of all
    # sis_ids seen across every Students CSV. See the "district-wide
    # withdrawal diff" block in workers/clever_import_job.py.
    #
    # Caller uses `sis_ids_seen` from the return payload to accumulate
    # across CSVs.
    removed = 0  # Filled in by the caller's district-wide pass.

    # Finalize import record
    imp.added = added
    imp.updated = updated
    imp.removed = removed
    imp.errors = errors
    imp.error_detail = "\n".join(error_lines[:50]) if error_lines else None
    imp.completed_at = datetime.now(timezone.utc)
    imp.status = "complete" if errors == 0 else "complete_with_errors"

    return {
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "removed": removed,
        "errors": errors,
        "withdrawn": [],           # Deferred to district-wide pass
        "sis_ids_seen": sis_ids_seen,
    }


# ── Export ───────────────────────────────────────────────────────────────

# T0.4 field allowlist for exports — admin only
EXPORT_FIELDS = [
    "sis_id", "first_name", "last_name", "middle_name",
    "email", "school", "grade", "status",
    "enrollment_date", "withdrawal_date",
    "google_status", "email_compliant",
]


async def export_students_csv(
    db: AsyncSession,
    *,
    school: str | None = None,
    status: str | None = None,
    actor: str,
    ip_address: str | None = None,
) -> str:
    """
    Export student roster as CSV with T0.4 field minimization.
    Admin-only, audited. Returns CSV string.
    """
    students, total = await repo.search_students(
        db, school=school, status=status or "active", limit=10000,
    )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
    writer.writeheader()

    for s in students:
        writer.writerow({f: getattr(s, f, "") for f in EXPORT_FIELDS})

    await audit_student_export(
        db,
        actor=actor,
        export_type="csv",
        record_count=len(students),
        scope=school or "district",
        ip_address=ip_address,
    )

    return output.getvalue()


# ── Student account provisioning ─────────────────────────────────────────

async def provision_student_account(
    db: AsyncSession,
    *,
    student_id: int,
    actor: str,
    auto_confirm_exact: bool = False,
) -> dict:
    """
    Provision or reactivate a Google Workspace account for a student.

    Flow:
    1. Check toggles (provisioning enabled, Google writes enabled)
    2. Build expected email from SIS data
    3. Check if email exists in Google:
       - Not found → create new account
       - Found → check SID match:
         - SID matches → returning student, unsuspend + move to correct OU
         - No SID → fuzzy name check, flag for confirmation if close
         - Different SID → disambiguate email, create new
    4. Set SID as Employee ID in Google
    5. Verify correct OU for current school
    """
    from app.modules.settings.repository import get_setting_value
    from app.modules.roster.clever_service import build_expected_email, expected_grad_year
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    # ── Check mode ──
    # Single setting replaces the old (provisioning_enabled, google_writes_enabled)
    # pair. Legacy fallback: if mode is unset AND either legacy switch exists,
    # resolve from the pair so an upgrade mid-flight doesn't flip the deploy
    # from Autopilot to Off silently.
    mode = (await get_setting_value(db, "roster", "student_provisioning_mode") or "").lower()
    if not mode:
        legacy_prov = (await get_setting_value(db, "roster", "student_provisioning_enabled") or "").lower()
        legacy_writes = (await get_setting_value(db, "roster", "student_google_writes_enabled") or "").lower()
        if legacy_prov == "true" and legacy_writes == "true":
            mode = "autopilot"
        elif legacy_prov == "true":
            mode = "review"
        else:
            mode = "off"
    if mode == "off":
        return {"status": "skipped", "detail": "Account provisioning mode is Off"}
    if mode == "review":
        return {"status": "skipped", "detail": "Account provisioning mode is Review — queued for manual approval"}

    student = await repo.get_student_by_id(db, student_id)
    if not student:
        return {"status": "error", "detail": "Student not found"}

    domain = await get_setting_value(db, "google", "student_domain") or ""
    if not domain:
        return {"status": "error", "detail": "google.student_domain not configured"}

    # ── Resolve target OU from school code ──
    ou_map_raw = await get_setting_value(db, "roster", "student_ou_map") or "{}"
    try:
        ou_map = json.loads(ou_map_raw)
    except Exception:
        ou_map = {}
    target_ou = ou_map.get(student.school) or ou_map.get("*") or "/Users-Students"

    # ── Build expected email ──
    from app.modules.roster.email_format import EmailBuilder
    email_builder = await EmailBuilder.load(db)
    email = (student.email or "").strip().lower()
    if not email:
        grad_year = expected_grad_year(student.grade) if student.grade else None
        if grad_year and student.last_name and student.first_name:
            email = email_builder.build(student.last_name, student.first_name, grad_year, sid=student.sis_id)
            if not email:
                return {"status": "error", "detail": "Could not generate email (empty from format)"}
        else:
            return {"status": "error", "detail": "No email in SIS and cannot generate (missing name/grade)"}

    if not email.endswith(f"@{domain}"):
        return {"status": "skipped", "detail": f"Non-district email: {email}"}

    # ── Build password from template ──
    pwd_template = await get_setting_value(db, "roster", "student_default_password") or "0000{SID_LAST4}"
    sid_last4 = student.sis_id[-4:] if student.sis_id and len(student.sis_id) >= 4 else "0000"
    temp_password = pwd_template.replace("{SID_LAST4}", sid_last4).replace("{SID}", student.sis_id or "")

    google = GoogleWorkspaceAdapter(db)
    target_name = f"{student.first_name[0]}. {student.last_name} ({student.sis_id})"
    before_state = {"sis_id": student.sis_id, "google_status": student.google_status, "email": student.email}

    try:
        # ── Check if email already exists in Google ──
        existing = await google.check_email_exists(email)

        if existing:
            # Email exists — check if it's the same student returning
            accounts = await google.list_users(query=f"email={email}", max_results=1)
            if not accounts:
                return {"status": "error", "detail": f"Email {email} exists but cannot fetch details"}

            g_account = accounts[0]
            g_orgs = g_account.get("organizations") or [{}]
            g_employee_id = None
            for org in g_orgs if isinstance(g_orgs, list) else [g_orgs]:
                if isinstance(org, dict):
                    g_employee_id = org.get("employeeId") or org.get("employee_id")
                    if g_employee_id:
                        break
            # Also check externalIds
            for ext in g_account.get("external_ids") or g_account.get("externalIds") or []:
                if isinstance(ext, dict) and ext.get("type") == "organization":
                    g_employee_id = g_employee_id or ext.get("value")

            g_name = g_account.get("full_name") or f"{g_account.get('first_name','')} {g_account.get('last_name','')}".strip()
            g_status = g_account.get("status", "")
            g_ou = g_account.get("org_unit", "")

            if g_employee_id and g_employee_id == student.sis_id:
                # ── Same student returning ──
                actions = []

                # Unsuspend if needed
                if g_status == "suspended":
                    unsuspend = await google.reactivate_account(email)
                    if unsuspend.success:
                        actions.append("unsuspended")

                # Move to correct OU if wrong
                if g_ou != target_ou:
                    move = await google.move_to_ou(email, target_ou)
                    if move.success:
                        actions.append(f"moved from {g_ou} to {target_ou}")

                student.google_status = "active"
                await log_action(db, actor=actor, action="roster.accounts.reactivate", module="roster",
                    target=target_name, details=json.dumps({"actions": actions, "before_ou": g_ou}))
                return {"status": "reactivated", "email": email, "actions": actions}

            elif g_employee_id and g_employee_id != student.sis_id:
                # ── Different student with same email — apply the
                # operator's configured collision strategy.
                new_email = email_builder.build(
                    student.last_name, student.first_name,
                    expected_grad_year(student.grade) or 0,
                    sid=student.sis_id, attempt=1,
                )
                if new_email is None:
                    # Collision strategy = reject_for_review
                    return {"status": "error", "detail": f"Email {email} is taken by SID {g_employee_id} — collision strategy is set to reject; please review manually"}

                if await google.check_email_exists(new_email):
                    return {"status": "error", "detail": f"Both {email} and {new_email} already exist in Google"}

                # Create with disambiguated email
                result = await google.create_account(email=new_email, first_name=student.first_name,
                    last_name=student.last_name, org_unit=target_ou, temp_password=temp_password)
                if result.success:
                    await google.set_student_id(new_email, student.sis_id)
                    student.google_status = "active"
                    student.email = new_email
                    await log_action(db, actor=actor, action="roster.accounts.provision", module="roster",
                        target=target_name, details=json.dumps({"disambiguated": True, "email": new_email, "original_taken_by": g_employee_id}))
                    return {"status": "ok", "email": new_email, "disambiguated": True}
                return {"status": "error", "detail": result.error}

            else:
                # ── No SID on existing account — fuzzy name check ──
                s_name = f"{student.first_name} {student.last_name}".lower().strip()
                g_name_lower = g_name.lower().strip()

                if s_name == g_name_lower:
                    # Exact name match. Normally we flag for human review
                    # (an untouchable safety on account merges), but when
                    # the caller opts into auto_confirm_exact — typically a
                    # backfill run for accounts predating SID stamping —
                    # we treat "same name, no SID" as "same person" and
                    # perform the link automatically: stamp employeeId,
                    # unsuspend if needed, move to the right OU.
                    if auto_confirm_exact:
                        actions = []
                        try:
                            await google.set_student_id(email, student.sis_id)
                            actions.append("stamped SID")
                        except Exception as e:
                            logger.warning(f"set_student_id failed for {email}: {e}")
                        if g_status == "suspended":
                            unsuspend = await google.reactivate_account(email)
                            if unsuspend.success:
                                actions.append("unsuspended")
                        if g_ou != target_ou:
                            move = await google.move_to_ou(email, target_ou)
                            if move.success:
                                actions.append(f"moved from {g_ou} to {target_ou}")
                        student.google_status = "active"
                        student.email = email
                        await log_action(
                            db, actor=actor, action="roster.accounts.auto_linked",
                            module="roster", target=target_name,
                            details=json.dumps({
                                "email": email, "google_name": g_name,
                                "actions": actions, "before_ou": g_ou,
                                "trigger": "auto_confirm_exact",
                            }),
                        )
                        return {"status": "reactivated", "email": email, "actions": actions, "auto_linked": True}
                    return {
                        "status": "confirm_needed",
                        "detail": f"Email {email} exists with matching name '{g_name}' but no SID. Confirm to link.",
                        "google_name": g_name,
                        "google_ou": g_ou,
                        "google_status": g_status,
                    }
                else:
                    # Names don't match exactly — treat as a different
                    # person and disambiguate. Historically we flagged
                    # "close" matches (first-3-chars-of-first + last
                    # names both matched) for manual review, but that
                    # trap misfires on siblings (Malkom vs Malaki Allen)
                    # far more often than it saves a Rachel/Rachael
                    # spelling variant. The disambiguation path is safe
                    # and reversible; a wrong flag is not.
                    new_email = email_builder.build(
                        student.last_name, student.first_name,
                        expected_grad_year(student.grade) or 0,
                        sid=student.sis_id, attempt=1,
                    )
                    if new_email is None:
                        return {"status": "error", "detail": f"Email {email} taken by same-name (no SID) and collision strategy set to reject; please review manually"}
                    if await google.check_email_exists(new_email):
                        return {"status": "error", "detail": f"Both {email} and {new_email} exist. Manual intervention needed."}
                    result = await google.create_account(email=new_email, first_name=student.first_name,
                        last_name=student.last_name, org_unit=target_ou, temp_password=temp_password)
                    if result.success:
                        await google.set_student_id(new_email, student.sis_id)
                        student.google_status = "active"
                        student.email = new_email
                        await log_action(db, actor=actor, action="roster.accounts.provision", module="roster",
                            target=target_name, details=json.dumps({"disambiguated": True, "email": new_email, "existing_name": g_name}))
                        return {"status": "ok", "email": new_email, "disambiguated": True}
                    return {"status": "error", "detail": result.error}

        else:
            # ── Email doesn't exist — create new account ──
            result = await google.create_account(
                email=email,
                first_name=student.first_name,
                last_name=student.last_name,
                org_unit=target_ou,
                temp_password=temp_password,
            )
            if result.success:
                await google.set_student_id(email, student.sis_id)
                student.google_status = "active"
                student.email = email
                await log_action(db, actor=actor, action="roster.accounts.provision", module="roster",
                    target=target_name, details=json.dumps({"before": before_state, "email": email, "ou": target_ou}))
                return {"status": "ok", "email": email}
            return {"status": "error", "detail": result.error}

    except Exception as e:
        logger.error(f"Student account provision failed for {student.sis_id}: {e}")
        return {"status": "error", "detail": str(e)[:200]}


# ── Compliance export ────────────────────────────────────────────────────

COMPLIANCE_EXPORT_FIELDS = [
    "StudentID", "StudentName", "SchoolCode", "CleverEmail",
    "GoogleEmail", "ExpectedEmail", "Issue", "DetectedDate",
]


async def export_compliance_csv(
    db: AsyncSession,
    *,
    actor: str,
    ip_address: str | None = None,
) -> str:
    """Export email compliance violations as CSV. Admin-only, audited."""
    changes = await repo.list_roster_changes(
        db, change_type="email_noncompliant",
    )
    if not changes:
        return ""

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(COMPLIANCE_EXPORT_FIELDS)
    for r in changes:
        details = json.loads(r.details) if r.details else {}
        writer.writerow([
            r.student_id or "",
            r.student_name or "",
            r.school_code or "",
            details.get("actual", ""),
            details.get("google_email", ""),
            details.get("expected", ""),
            details.get("reason", ""),
            r.created_at.strftime("%Y-%m-%d") if r.created_at else "",
        ])

    await audit_student_export(
        db,
        actor=actor,
        export_type="compliance_csv",
        record_count=len(changes),
        scope="district",
        ip_address=ip_address,
    )
    return output.getvalue()


MISSING_EMAIL_EXPORT_FIELDS = [
    "StudentID", "StudentName", "SchoolCode", "SuggestedEmail",
    "GoogleHasIt", "DetectedDate",
]


async def export_missing_emails_csv(
    db: AsyncSession,
    *,
    actor: str,
    ip_address: str | None = None,
) -> str:
    """Export students with missing emails as CSV. Admin-only, audited."""
    changes = await repo.list_roster_changes(
        db, change_type="missing_email",
    )
    if not changes:
        return ""

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(MISSING_EMAIL_EXPORT_FIELDS)
    for r in changes:
        details = json.loads(r.details) if r.details else {}
        writer.writerow([
            r.student_id or "",
            r.student_name or "",
            r.school_code or "",
            details.get("suggested_email", ""),
            "Yes" if details.get("google_has_it") else "No",
            r.created_at.strftime("%Y-%m-%d") if r.created_at else "",
        ])

    await audit_student_export(
        db,
        actor=actor,
        export_type="missing_email_csv",
        record_count=len(changes),
        scope="district",
        ip_address=ip_address,
    )
    return output.getvalue()


_ADDR_RE = re.compile(
    r"""^\s*
        (?P<street>.+?)\s*,\s*
        (?P<city>[^,]+?)\s*,\s*
        (?P<state>[A-Za-z]{2})\s+
        (?P<zip>\d{5}(?:-\d{4})?)\s*$
    """,
    re.VERBOSE,
)


def _parse_address(blob: str) -> dict:
    """
    Split a SIS address string like
    ``"1646 Jackson St, the district, OH 45662"`` into individual
    street/city/state/zip components for NutriKids' 4-column layout.
    Handles 5-digit and 9-digit ZIPs; multi-word cities ("Mc Dermott")
    survive because they're already comma-bounded in the source.

    Malformed / missing addresses fall back to dumping the whole blob
    into ``street`` and leaving the others blank — NutriKids will
    reject the row on import if it strictly requires those fields,
    which is the correct loud-fail signal to fix the source data.
    """
    if not blob:
        return {"street": "", "city": "", "state": "", "zip": ""}
    m = _ADDR_RE.match(blob)
    if m:
        return {
            "street": m.group("street").strip(),
            "city": m.group("city").strip(),
            "state": m.group("state").upper(),
            "zip": m.group("zip"),
        }
    return {"street": blob.strip(), "city": "", "state": "", "zip": ""}


def _snapshot_to_nutrikids_dict(s) -> dict:
    """
    Convert a RosterSnapshot row into the dict shape
    ``build_nutrikids_rows`` expects. The Clever CSV path stores rich
    per-contact structured data in ``_contacts``; snapshot rows only
    have the flat parent_guardian + phone + address fields, so we emit
    a single synthetic contact using those.
    """
    student_num = (s.sis_id or "").strip()
    contact_last = (s.parent_guardian or "").strip()
    phone = (s.phone or "").strip()
    addr = _parse_address((s.address or "").strip())
    return {
        "Student_id": student_num,
        "First_name": s.first_name or "",
        "Last_name": s.last_name or "",
        "Middle_name": s.middle_name or "",
        "DOB": s.dob or "",
        "Grade": s.grade or "",
        "School_id": s.school or "",
        "School_name": s.school or "",  # snapshot doesn't store display name; downstream can rename
        "Homeroom": "",
        "Homeroom_id": "",
        "Teacher_display_name": "",
        "_contacts": [{
            "title": "",
            "last_name": contact_last,
            "suffix": "",
            "street": addr["street"],
            "street2": "",
            "city": addr["city"],
            "state": addr["state"],
            "zip": addr["zip"],
            "home_phone": phone,
            "mobile_phone": "",
            "email": "",
        }] if (contact_last or addr["street"] or phone) else [{}],
    }


async def export_nutrikids_csv(
    db: AsyncSession,
    *,
    days: int = 30,
    full: bool = False,
    actor: str,
    ip_address: str | None = None,
) -> str:
    """
    Export NutriKids-format CSV.

    Two modes:
      - Diff (default): recently ``added`` students from roster_changes
        within the ``days`` window. Good for weekly incremental uploads.
      - Full (``full=True``): every active student in roster_snapshots.
        Used for start-of-year initial NutriKids setup.

    Admin-only, audited either way.
    """
    from datetime import timedelta
    from app.modules.roster.clever_service import generate_nutrikids_csv
    from app.modules.roster.models import RosterSnapshot
    from sqlalchemy import select

    students: list[dict] = []
    scope = "district_full" if full else "district"

    if full:
        # Every active student, no date filter, no diff.
        rows = (await db.execute(
            select(RosterSnapshot).where(RosterSnapshot.status == "active")
            .order_by(RosterSnapshot.school, RosterSnapshot.last_name, RosterSnapshot.first_name)
        )).scalars().all()
        students = [_snapshot_to_nutrikids_dict(s) for s in rows]
    else:
        since = datetime.now(timezone.utc) - timedelta(days=min(days, 365))
        changes = await repo.list_roster_changes(
            db, change_type="added", since=since,
        )
        for c in changes:
            details = json.loads(c.details) if c.details else {}
            student_data = details.get("student_data", {})
            if student_data:
                students.append(student_data)

    csv_content = generate_nutrikids_csv(students)

    await audit_student_export(
        db,
        actor=actor,
        export_type="nutrikids_csv_full" if full else "nutrikids_csv",
        record_count=len(students),
        scope=scope,
        ip_address=ip_address,
    )

    return csv_content
