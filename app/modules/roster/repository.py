"""
Roster repository — DB access for student roster, imports, class lists, guidance queue.

No commit inside — routers and workers own transactions.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.roster.models import (
    RosterImport, RosterSnapshot, StudentTeacher, GuidanceQueue,
    RosterChange, GoogleChangeLog, EmailIgnore,
)

logger = logging.getLogger(__name__)


# ── Imports ──────────────────────────────────────────────────────────────

async def create_import(db: AsyncSession, source: str, started_by: str, filename: str | None = None) -> RosterImport:
    """Stage a new import record. Caller must commit."""
    imp = RosterImport(source=source, started_by=started_by, filename=filename)
    db.add(imp)
    await db.flush()
    return imp


async def get_import(db: AsyncSession, import_id: int) -> RosterImport | None:
    result = await db.execute(select(RosterImport).where(RosterImport.id == import_id))
    return result.scalar_one_or_none()


async def list_imports(db: AsyncSession, limit: int = 20) -> list[RosterImport]:
    result = await db.execute(
        select(RosterImport).order_by(desc(RosterImport.started_at)).limit(limit)
    )
    return list(result.scalars().all())


# ── Snapshots (student directory) ────────────────────────────────────────

async def get_student_by_sis_id(db: AsyncSession, sis_id: str) -> RosterSnapshot | None:
    result = await db.execute(select(RosterSnapshot).where(RosterSnapshot.sis_id == sis_id))
    return result.scalar_one_or_none()


async def get_student_by_id(db: AsyncSession, student_id: int) -> RosterSnapshot | None:
    result = await db.execute(select(RosterSnapshot).where(RosterSnapshot.id == student_id))
    return result.scalar_one_or_none()


async def upsert_student(db: AsyncSession, *, sis_id: str, import_id: int | None = None, **fields) -> tuple[RosterSnapshot, str]:
    """
    Insert or update a student record by SIS ID.
    Returns (snapshot, action) where action is "added" | "updated" | "unchanged".
    Caller must commit.
    """
    existing = await get_student_by_sis_id(db, sis_id)
    if existing:
        changed = False
        for key, value in fields.items():
            if hasattr(existing, key) and getattr(existing, key) != value:
                setattr(existing, key, value)
                changed = True
        if import_id:
            existing.import_id = import_id
        existing.last_seen_at = datetime.now(timezone.utc)
        return existing, "updated" if changed else "unchanged"

    student = RosterSnapshot(sis_id=sis_id, import_id=import_id, **fields)
    db.add(student)
    await db.flush()
    return student, "added"


async def search_students(
    db: AsyncSession,
    *,
    school: str | None = None,
    grade: str | None = None,
    status: str | None = None,
    search: str | None = None,
    issue: str | None = None,
    days: int | None = None,
    enrolled_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RosterSnapshot], int]:
    """
    Search student directory with filters. Returns (students, total_count).
    Scoping by building is enforced at the router level.
    enrolled_only=True (default) filters to students with section assignments.
    """
    q = select(RosterSnapshot)

    if school:
        q = q.where(RosterSnapshot.school == school)
    if grade:
        q = q.where(RosterSnapshot.grade == grade)
    if status:
        q = q.where(RosterSnapshot.status == status)
    if search:
        # Split into tokens — each token must match at least one field
        from sqlalchemy import and_
        tokens = search.strip().split()
        for token in tokens:
            term = f"%{token}%"
            q = q.where(or_(
                RosterSnapshot.first_name.ilike(term),
                RosterSnapshot.last_name.ilike(term),
                RosterSnapshot.sis_id.ilike(term),
                RosterSnapshot.email.ilike(term),
            ))
    if issue:
        if issue == "new":
            # Recently added students — filter by created_at within N days
            d = days or 7
            cutoff = datetime.now(timezone.utc) - timedelta(days=d)
            q = q.where(RosterSnapshot.created_at >= cutoff)
        elif issue == "changes":
            # Any roster change (added / removed / transferred / grade /
            # name) within the day window. Withdrawn students need to
            # show up here, so the enrolled_only filter is bypassed below.
            from app.modules.roster.models import RosterChange
            d = days or 7
            cutoff = datetime.now(timezone.utc) - timedelta(days=d)
            q = q.where(RosterSnapshot.sis_id.in_(
                select(RosterChange.student_id).where(
                    RosterChange.created_at >= cutoff,
                    RosterChange.change_type.in_([
                        "added", "removed", "transferred",
                        "grade_change", "name_change",
                    ]),
                )
            ))
        elif issue == "clever_duplicate":
            q = q.where(RosterSnapshot.email.op("~")(r"[0-9]{3,}@"))
        elif issue == "any":
            q = q.where(RosterSnapshot.issue_tags != None, RosterSnapshot.issue_tags != "")
        elif issue == "no_google":
            q = q.where(or_(RosterSnapshot.google_status == "missing", RosterSnapshot.google_status == None, RosterSnapshot.google_status == ""))
        elif issue == "wrong_domain":
            q = q.where(RosterSnapshot.issue_tags.ilike("%wrong_domain%"))
        else:
            q = q.where(RosterSnapshot.issue_tags.ilike(f"%{issue}%"))
    # The "changes" filter intentionally includes withdrawn (unenrolled)
    # students; skip the enrolled-only clamp in that case.
    if enrolled_only and issue != "changes":
        q = q.where(RosterSnapshot.id.in_(
            select(StudentTeacher.student_id).distinct()
        ))

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.order_by(RosterSnapshot.last_name, RosterSnapshot.first_name).offset(offset).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all()), total


async def mark_students_inactive(db: AsyncSession, sis_ids_seen: set[str], school: str, import_id: int | None = None) -> tuple[int, list[dict]]:
    """
    Mark students at a school as inactive if they weren't in the import.
    Returns (count, withdrawn_students) where withdrawn_students is a list
    of dicts with student info for downstream processing.
    Caller must commit.
    """
    result = await db.execute(
        select(RosterSnapshot).where(
            RosterSnapshot.school == school,
            RosterSnapshot.status == "active",
        )
    )
    count = 0
    withdrawn = []
    for student in result.scalars().all():
        if student.sis_id not in sis_ids_seen:
            student.status = "inactive"
            count += 1
            withdrawn.append({
                "id": student.id,
                "sis_id": student.sis_id,
                "first_name": student.first_name,
                "last_name": student.last_name,
                "email": student.email,
                "school": student.school,
                "grade": student.grade,
            })
            # Log the withdrawal as a roster change
            if import_id:
                await create_roster_change(
                    db,
                    import_id=import_id,
                    student_id=student.sis_id,
                    change_type="removed",
                    student_name=f"{student.first_name} {student.last_name}".strip(),
                    school_code=school,
                    details=f'{{"last_school": "{school}", "email": "{student.email or ""}"}}',
                )
    return count, withdrawn


# ── Class lists (student_teachers) ───────────────────────────────────────

async def get_class_list(
    db: AsyncSession,
    *,
    school: str | None = None,
    teacher_email: str | None = None,
    teacher_name: str | None = None,
    section_name: str | None = None,
    include_contacts: bool = False,
) -> list[dict]:
    """
    Class list for a building or teacher's sections.

    Base fields per T0.4: name, grade, teacher, section.
    Contact fields (parent_guardian, phone) included only when
    include_contacts=True — never address, DOB, or account state.

    teacher_email: filter to a specific teacher's assigned sections.
    school: filter to a building (required unless teacher_email is set).
    """
    cols = [
        RosterSnapshot.first_name,
        RosterSnapshot.last_name,
        RosterSnapshot.grade,
        RosterSnapshot.sis_id,
        StudentTeacher.teacher_name,
        StudentTeacher.section_name,
        StudentTeacher.homeroom,
        StudentTeacher.period,
    ]
    if include_contacts:
        cols += [RosterSnapshot.parent_guardian, RosterSnapshot.phone]

    q = (
        select(*cols)
        .join(StudentTeacher, StudentTeacher.student_id == RosterSnapshot.id)
        .where(RosterSnapshot.status == "active")
    )

    if school:
        q = q.where(StudentTeacher.school == school)
    if teacher_email:
        q = q.where(StudentTeacher.teacher_email == teacher_email)
    if teacher_name:
        q = q.where(StudentTeacher.teacher_name.ilike(f"%{teacher_name}%"))
    if section_name:
        q = q.where(StudentTeacher.section_name.ilike(f"%{section_name}%"))

    q = q.order_by(StudentTeacher.teacher_name, RosterSnapshot.last_name)
    result = await db.execute(q)

    rows = []
    for row in result.all():
        entry = {
            "first_name": row.first_name,
            "last_name": row.last_name,
            "grade": row.grade,
            "sis_id": row.sis_id,
            "teacher_name": row.teacher_name,
            "section_name": row.section_name,
            "homeroom": row.homeroom,
            "period": row.period,
        }
        if include_contacts:
            entry["parent_guardian"] = row.parent_guardian
            entry["phone"] = row.phone
        rows.append(entry)
    return rows


async def replace_teacher_assignments(
    db: AsyncSession,
    student_id: int,
    school: str,
    assignments: list[dict],
) -> None:
    """Replace all teacher assignments for a student. Caller must commit."""
    # Delete existing
    existing = await db.execute(
        select(StudentTeacher).where(StudentTeacher.student_id == student_id)
    )
    for row in existing.scalars().all():
        await db.delete(row)
    await db.flush()

    # Insert new
    for a in assignments:
        db.add(StudentTeacher(
            student_id=student_id,
            teacher_name=a["teacher_name"],
            teacher_email=a.get("teacher_email"),
            section_name=a.get("section_name"),
            homeroom=a.get("homeroom"),
            period=a.get("period"),
            school=school,
        ))


# ── Guidance queue ───────────────────────────────────────────────────────

async def create_queue_item(db: AsyncSession, **kwargs) -> GuidanceQueue:
    """Stage a guidance queue item. Caller must commit."""
    item = GuidanceQueue(**kwargs)
    db.add(item)
    await db.flush()
    return item


async def get_queue_item(db: AsyncSession, item_id: int) -> GuidanceQueue | None:
    result = await db.execute(select(GuidanceQueue).where(GuidanceQueue.id == item_id))
    return result.scalar_one_or_none()


async def list_queue_items(
    db: AsyncSession,
    *,
    school: str | None = None,
    status: str | None = None,
    assigned_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[GuidanceQueue], int]:
    """List guidance queue items with filters. Returns (items, total)."""
    q = select(GuidanceQueue)
    if school:
        q = q.where(GuidanceQueue.school == school)
    if status:
        q = q.where(GuidanceQueue.status == status)
    if assigned_to:
        q = q.where(GuidanceQueue.assigned_to == assigned_to)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.order_by(desc(GuidanceQueue.created_at)).offset(offset).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all()), total


async def get_roster_summary(db: AsyncSession, school: str | None = None) -> dict:
    """Summary stats for dashboard/roster overview."""
    base = select(RosterSnapshot)
    if school:
        base = base.where(RosterSnapshot.school == school)

    total = (await db.execute(
        select(func.count()).select_from(base.where(RosterSnapshot.status == "active").subquery())
    )).scalar_one()

    schools = (await db.execute(
        select(RosterSnapshot.school).where(RosterSnapshot.status == "active").distinct()
    )).all()

    queue_open = (await db.execute(
        select(func.count()).select_from(
            select(GuidanceQueue).where(GuidanceQueue.status.in_(["open", "in_progress"])).subquery()
        )
    )).scalar_one()

    last_import = (await db.execute(
        select(RosterImport).order_by(desc(RosterImport.completed_at)).limit(1)
    )).scalar_one_or_none()

    return {
        "total_active_students": total,
        "schools": len(schools),
        "open_queue_items": queue_open,
        "last_import": last_import.completed_at.isoformat() if last_import and last_import.completed_at else None,
    }


async def student_matches_contact_scope(
    db: AsyncSession,
    *,
    student_id: int,
    scope_type: str,
    scope_value: str,
) -> bool:
    """
    Check whether a student falls inside a teacher/section contact scope.

    Used by _can_view_student_contacts to enforce section-scoped T4 access
    for teachers. Returns True only if the student is in the teacher's
    assigned section per the student_teachers table.
    """
    q = select(StudentTeacher).where(StudentTeacher.student_id == student_id)

    if scope_type == "teacher":
        q = q.where(StudentTeacher.teacher_email == scope_value)
    elif scope_type == "section":
        q = q.where(StudentTeacher.section_name == scope_value)
    else:
        return False

    result = await db.execute(q)
    return result.scalar_one_or_none() is not None


async def get_contact_map_for_sis_ids(
    db: AsyncSession,
    sis_ids: list[str],
) -> dict[str, dict]:
    """
    Fetch T4 contact fields for a list of SIS IDs.
    Returns {sis_id: {parent_guardian, phone}} for efficient class list overlay.
    Only fetches the two contact fields teachers are permitted to see —
    address is excluded per T0.4 teacher contact scope.
    """
    if not sis_ids:
        return {}
    result = await db.execute(
        select(
            RosterSnapshot.sis_id,
            RosterSnapshot.parent_guardian,
            RosterSnapshot.phone,
        ).where(RosterSnapshot.sis_id.in_(sis_ids))
    )
    return {
        row.sis_id: {
            "parent_guardian": row.parent_guardian,
            "phone": row.phone,
        }
        for row in result.all()
    }


# ── Roster changes ──────────────────────────────────────────────────────

async def create_roster_change(db: AsyncSession, **kwargs) -> RosterChange:
    """Stage a roster change record. Caller must commit."""
    change = RosterChange(**kwargs)
    db.add(change)
    await db.flush()
    return change


async def list_roster_changes(
    db: AsyncSession,
    *,
    change_types: set[str] | None = None,
    school_code: str | None = None,
    change_type: str | None = None,
    since: "datetime | None" = None,
    limit: int = 500,
) -> list[RosterChange]:
    """List roster changes with filters."""
    q = select(RosterChange).order_by(desc(RosterChange.created_at))
    if change_types:
        q = q.where(RosterChange.change_type.in_(change_types))
    if change_type:
        q = q.where(RosterChange.change_type == change_type)
    if school_code and school_code != "ALL":
        q = q.where(RosterChange.school_code == school_code)
    if since:
        q = q.where(RosterChange.created_at >= since)
    q = q.limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_unprovisioned_added(
    db: AsyncSession,
    school_code: str | None = None,
) -> list[RosterChange]:
    """Get added students not yet Google-provisioned."""
    from sqlalchemy import and_
    q = select(RosterChange).where(
        and_(
            RosterChange.change_type == "added",
            RosterChange.google_provisioned == False,
        )
    ).order_by(desc(RosterChange.created_at))
    if school_code and school_code != "ALL":
        q = q.where(RosterChange.school_code == school_code)
    result = await db.execute(q)
    return list(result.scalars().all())


# ── Google change log ───────────────────────────────────────────────────

async def create_google_change(db: AsyncSession, **kwargs) -> GoogleChangeLog:
    """Stage a Google change log entry. Caller must commit."""
    entry = GoogleChangeLog(**kwargs)
    db.add(entry)
    await db.flush()
    return entry


async def list_google_changes(
    db: AsyncSession,
    *,
    days: int = 60,
    action: str | None = None,
) -> list[GoogleChangeLog]:
    """List Google Workspace change log entries."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=min(days, 365))
    q = (
        select(GoogleChangeLog)
        .where(GoogleChangeLog.changed_at >= cutoff)
        .order_by(desc(GoogleChangeLog.changed_at))
    )
    if action:
        q = q.where(GoogleChangeLog.action == action)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_unrevert_changes_after(
    db: AsyncSession,
    revert_to: "datetime",
) -> list[GoogleChangeLog]:
    """Get unrevert Google changes after a given datetime, newest first."""
    q = (
        select(GoogleChangeLog)
        .where(GoogleChangeLog.changed_at > revert_to)
        .where(GoogleChangeLog.reverted == False)
        .order_by(desc(GoogleChangeLog.changed_at))
    )
    result = await db.execute(q)
    return list(result.scalars().all())


# ── Email ignores ───────────────────────────────────────────────────────

async def get_active_ignored_emails(db: AsyncSession) -> set[str]:
    """Get the set of ignored email addresses (not yet restored)."""
    result = await db.execute(
        select(EmailIgnore).where(EmailIgnore.restored_at == None)
    )
    return {e.email.lower() for e in result.scalars().all()}


async def get_email_ignore(db: AsyncSession, email: str) -> EmailIgnore | None:
    result = await db.execute(
        select(EmailIgnore).where(EmailIgnore.email == email)
    )
    return result.scalar_one_or_none()


async def create_or_restore_email_ignore(
    db: AsyncSession,
    email: str,
    reason: str,
    ignored_by: str,
) -> EmailIgnore:
    """Add or re-activate an email ignore entry. Caller must commit."""
    existing = await get_email_ignore(db, email)
    if existing:
        existing.restored_at = None
        existing.reason = reason
        return existing
    entry = EmailIgnore(email=email, reason=reason, ignored_by=ignored_by)
    db.add(entry)
    await db.flush()
    return entry
