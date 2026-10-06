"""
Automated guidance queue — creates entries for new enrollments without schedules.

Triggered after each Clever roster import. Students who are newly added
and have no sections assigned get queued for counselor review.

Counselor routing is configured in Settings → Guidance:
  - group_rules: SCHOOL:GRADES=group_name (determines which group handles which students)
  - counselor_groups: group_name=email1,email2 (who gets notified)
"""

import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import text, select, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _grade_to_number(grade: str) -> int | None:
    """Convert grade string to number for comparison."""
    g = (grade or "").strip().upper()
    if g in ("PK", "PRE-K", "PREK"):
        return -1
    if g in ("K", "KG"):
        return 0
    try:
        return int(g)
    except ValueError:
        return None


def _resolve_counselor_group(school: str, grade: str, rules: list[dict]) -> str | None:
    """
    Determine which counselor group handles a student.

    Rules format: [{schools (set of SIS + internal codes), grades, group_name}]
    """
    grade_num = _grade_to_number(grade)
    school_upper = (school or "").upper()

    for rule in rules:
        if school_upper not in rule["schools"]:
            continue
        if rule["grades"] == "*":
            return rule["group_name"]
        if grade_num is not None and grade_num in rule["grade_nums"]:
            return rule["group_name"]
        if (grade or "").strip().upper() in rule["grades_raw"]:
            return rule["group_name"]

    return None



async def auto_queue_new_enrollments(db: AsyncSession, *, force: bool = False) -> dict:
    """
    Check for newly added students without sections and create guidance queue entries.

    Called automatically after each Clever import completes, and also
    manually from an admin "Queue New Enrollees" button in the roster
    UI. The ``force`` parameter is retained for API compatibility but
    is no longer consulted for queue insertion — queue writes always
    happen, scoped by the recent-days window. The flag lives now on
    the NOTIFICATION side (see ``_send_guidance_notification``).

    Scoped to students whose ``roster_snapshots.created_at`` is within
    the last N days (default 3, configurable via
    ``guidance.auto_queue_recent_days`` setting). Without the date
    filter, this query would catch EVERY active student without
    sections — a population that includes resident-only students who
    attend non-district schools and will never have a section. The
    district's existing roster of ~2500 active students has ~860
    matching that broader criterion; the 3-day filter drops it to a
    manageable handful per import cycle.

    **Knob separation (as of a037 migration):**
      - Queue insertion: always on (this function)
      - Counselor email notifications: gated by ``guidance.notify_enabled``
      - Automated Google account creation: gated by
        ``roster.student_google_writes_enabled`` (separate module)
    """
    from app.modules.settings.repository import get_setting_value

    result = {"queued": 0, "auto_resolved": 0, "notified": [], "recent_days": None}

    # Recent-days window — configurable, default 3. Applied here so
    # turning up import frequency never avalanches the queue.
    days_str = await get_setting_value(db, "guidance", "auto_queue_recent_days") or "3"
    try:
        recent_days = max(1, int(days_str))
    except (ValueError, TypeError):
        recent_days = 3
    result["recent_days"] = recent_days

    # Load counselor groups config (JSON array)
    import json
    groups_raw = await get_setting_value(db, "guidance", "counselor_groups") or "[]"
    try:
        counselor_groups = json.loads(groups_raw)
    except (json.JSONDecodeError, TypeError):
        counselor_groups = []
    if not counselor_groups:
        return {"skipped": "No counselor groups configured"}

    # Load building map (SIS code → internal code) for translation
    # Guidance groups use internal codes (PES), roster uses SIS codes (SIS_A)
    import json
    bmap_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        bmap = json.loads(bmap_raw)  # {SIS_A: PES, SIS_B: PHS, SIS_C: EPE}
    except (json.JSONDecodeError, TypeError):
        bmap = {}
    # Build reverse: internal → SIS codes (PES → [SIS_A])
    reverse_map = {}
    for sis, internal in bmap.items():
        reverse_map.setdefault(internal.upper(), []).append(sis.upper())

    # Build routing rules from group config
    rules = []
    for g in counselor_groups:
        grade_list = g.get("grades", "")
        building = (g.get("building") or "").upper()
        if not building:
            continue
        # Match on both internal code AND any SIS codes that map to it
        school_codes = {building} | set(reverse_map.get(building, []))

        if grade_list == "*" or not grade_list:
            rules.append({"schools": school_codes, "grades": "*", "grade_nums": set(), "grades_raw": set(), "group_name": g.get("name", "")})
        else:
            grades_raw = {gr.strip().upper() for gr in str(grade_list).split(",") if gr.strip()}
            grade_nums = set()
            for gr in grades_raw:
                n = _grade_to_number(gr)
                if n is not None:
                    grade_nums.add(n)
            rules.append({"schools": school_codes, "grades": "list", "grade_nums": grade_nums, "grades_raw": grades_raw, "group_name": g.get("name", "")})

    # Build notification map: group_name → [emails]
    notify_map = {}
    for g in counselor_groups:
        name = g.get("name", "")
        emails = g.get("notify_emails", [])
        if isinstance(emails, str):
            emails = [e.strip() for e in emails.split(",") if e.strip() and "@" in e.strip()]
        if name and emails:
            notify_map[name] = emails

    # Find recently-added active students with no sections who
    # aren't already in the queue. The `created_at >= NOW() - N days`
    # filter is the avalanche guard — see the docstring above for
    # why it's necessary.
    new_students = await db.execute(text("""
        SELECT rs.id, rs.sis_id, rs.first_name, rs.last_name, rs.grade, rs.school, rs.email
        FROM roster_snapshots rs
        WHERE rs.status = 'active'
          AND rs.created_at >= NOW() - make_interval(days => :days)
          AND NOT EXISTS (
              SELECT 1 FROM student_teachers st WHERE st.student_id = rs.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM guidance_queue gq
              WHERE gq.student_id = rs.id AND gq.status IN ('open', 'in_progress')
          )
    """).bindparams(days=recent_days))
    rows = new_students.all()

    if not rows:
        logger.debug("Guidance auto-queue: no new students without sections")
        return result

    # Group by counselor group for batch notification
    by_group: dict[str, list] = {}
    now = datetime.now(timezone.utc)

    for r in rows:
        student_id, sis_id, first_name, last_name, grade, school, email = r
        group = _resolve_counselor_group(school, grade, rules)
        if not group:
            continue

        # Create queue entry
        await db.execute(text("""
            INSERT INTO guidance_queue (student_id, school, category, priority, status, created_by, created_at)
            VALUES (:sid, :school, 'enrollment', 'normal', 'open', 'system', :now)
        """).bindparams(sid=student_id, school=school, now=now))

        result["queued"] += 1
        by_group.setdefault(group, []).append({
            "name": f"{first_name} {last_name}",
            "sis_id": sis_id,
            "grade": grade,
            "school": school,
            "email": email or "",
        })

    # Send notifications per counselor group
    for group_name, students in by_group.items():
        recipients = notify_map.get(group_name, [])
        if not recipients:
            continue
        try:
            await _send_guidance_notification(db, group_name, recipients, students)
            result["notified"].append({"group": group_name, "count": len(students), "recipients": recipients})
        except Exception as e:
            logger.error(f"Guidance notification failed for {group_name}: {e}")

    logger.info(f"Guidance auto-queue: {result['queued']} students queued, {len(result['notified'])} groups notified")
    return result


async def auto_resolve_with_sections(db: AsyncSession) -> int:
    """
    Auto-resolve pending guidance entries where the student now has sections.

    Called after enrollment enrichment completes.
    """
    resolved = await db.execute(text("""
        UPDATE guidance_queue SET
            status = 'resolved',
            resolved_by = 'system',
            resolved_at = NOW()
        WHERE status = 'open'
          AND category = 'enrollment'
          AND EXISTS (
              SELECT 1 FROM student_teachers st WHERE st.student_id = guidance_queue.student_id
          )
    """))
    count = resolved.rowcount
    if count:
        logger.info(f"Guidance auto-resolve: {count} entries resolved (sections found)")
    return count


async def auto_queue_withdrawals(
    db: AsyncSession,
    withdrawn: list[dict],
    *,
    force: bool = False,
) -> dict:
    """
    Create guidance queue entries for withdrawn students.

    Called automatically from the Clever import flow after
    ``mark_students_inactive`` identifies removals, and also
    manually from ``auto_queue_recent_withdrawals`` (which builds
    its own withdrawn list by querying for recently-flipped
    ``status='inactive'`` rows).

    Queue insertion is always on; the ``force`` parameter is retained
    for API compatibility but no longer consulted. Counselor email
    notifications are gated separately by ``guidance.notify_enabled``
    in the enrollment path; withdrawal entries do not currently send
    emails (only enrollments do — see ``_send_guidance_notification``).
    """
    result = {"queued": 0}

    if not withdrawn:
        return result

    now = datetime.now(timezone.utc)

    for student in withdrawn:
        # Check not already queued
        existing = await db.execute(text(
            "SELECT id FROM guidance_queue WHERE student_id = :sid AND status IN ('open', 'in_progress') AND category = 'withdrawal'"
        ).bindparams(sid=student["id"]))
        if existing.first():
            continue

        await db.execute(text("""
            INSERT INTO guidance_queue (student_id, school, category, priority, status, created_by, created_at,
                notes)
            VALUES (:sid, :school, 'withdrawal', 'high', 'open', 'system', :now,
                :notes)
        """).bindparams(
            sid=student["id"],
            school=student["school"],
            now=now,
            notes=f"Student dropped from Clever roster. Email: {student.get('email', 'none')}",
        ))
        result["queued"] += 1

    if result["queued"]:
        logger.info(f"Guidance auto-queue: {result['queued']} withdrawal entries created")

    return result


async def _send_guidance_notification(db: AsyncSession, group_name: str, recipients: list[str], students: list[dict]):
    """
    Send email notification to counselor group about new queue entries.

    Gated by ``guidance.notify_enabled`` — when false, queue entries
    still land in the database (so counselors can see them in the UI
    on their next login) but no email is sent. This is the knob
    operators flip when they want to "start filling the queue
    without spamming counselors yet." Default: false (disabled).
    """
    from app.modules.settings.repository import get_setting_value
    import asyncio

    enabled = await get_setting_value(db, "guidance", "notify_enabled")
    if not enabled or enabled.lower() != "true":
        logger.debug(
            "Guidance notify skipped for %s (%d students) — "
            "notify_enabled is not true",
            group_name, len(students),
        )
        return

    subject_tpl = await get_setting_value(db, "guidance", "notify_subject") or "{count} New Student(s) Awaiting Schedule — {group}"
    sender = await get_setting_value(db, "google", "admin_email")
    if not sender:
        return

    count = len(students)
    school = students[0]["school"] if students else ""
    subject = subject_tpl.replace("{count}", str(count)).replace("{school}", school).replace("{group}", group_name)

    district_name = await get_setting_value(db, "branding", "district_name") or "Command"
    app_url_base = (await get_setting_value(db, "branding", "app_url") or "").rstrip("/")

    # Build student table — T0.4: initials + last name only
    table_rows = "".join(
        f'<tr><td style="padding:8px 12px;border:1px solid #30363d">{s["name"].split()[0][:1]}. {s["name"].split()[-1]}</td>'
        f'<td style="padding:8px 12px;border:1px solid #30363d">{s["grade"]}</td>'
        f'<td style="padding:8px 12px;border:1px solid #30363d">{s["school"]}</td></tr>'
        for s in students
    )
    student_table = (
        f'<table style="border-collapse:collapse;font-size:14px;font-family:Arial,sans-serif;width:100%">'
        f'<thead><tr style="background:#161b22">'
        f'<th style="padding:8px 12px;border:1px solid #30363d;color:#8b949e;text-align:left;font-size:12px;text-transform:uppercase">Student</th>'
        f'<th style="padding:8px 12px;border:1px solid #30363d;color:#8b949e;text-align:left;font-size:12px;text-transform:uppercase">Grade</th>'
        f'<th style="padding:8px 12px;border:1px solid #30363d;color:#8b949e;text-align:left;font-size:12px;text-transform:uppercase">School</th>'
        f'</tr></thead><tbody>{table_rows}</tbody></table>'
    )

    # App link button
    app_url = f"{app_url_base}/roster" if app_url_base else ""
    link_button = (
        f'<div style="margin-top:24px;text-align:center">'
        f'<a href="{app_url}" style="display:inline-block;padding:10px 24px;background:#58a6ff;color:#fff;text-decoration:none;border-radius:6px;font-size:14px;font-weight:600">Open Guidance Queue</a>'
        f'</div>'
    ) if app_url else ""

    body_tpl = await get_setting_value(db, "guidance", "notify_body") or (
        '<p style="color:#8b949e;font-size:13px">{group} &mdash; The following students have been enrolled but do not have a class schedule assigned.</p>'
        '{student_table}'
        '{link}'
    )
    body = (
        body_tpl
        .replace("{count}", str(count))
        .replace("{school}", school)
        .replace("{group}", group_name)
        .replace("{student_table}", student_table)
        .replace("{link}", link_button)
    )

    # Wrap in branded email template
    footer = (
        f'<p style="margin:0;font-size:11px;color:#8b949e">You are receiving this because you are assigned to a guidance counselor group in {district_name} Nexus. '
        f'2-Step Verification must be enabled on your Google account to access the system. If you need assistance, contact IT.</p>'
    )
    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:600px">'
        f'<div style="background:#0d1117;padding:16px 20px;border-radius:8px 8px 0 0;border-bottom:2px solid #58a6ff">'
        f'<span style="color:#58a6ff;font-size:13px;font-weight:600;letter-spacing:0.05em">{district_name.upper()} NEXUS</span>'
        f'</div>'
        f'<div style="background:#161b22;padding:24px 20px;color:#e6edf3">'
        f'<h2 style="margin:0 0 8px;font-size:18px;color:#e6edf3">{count} New Student(s) Awaiting Schedule</h2>'
        f'{body}'
        f'</div>'
        f'<div style="background:#0d1117;padding:16px 20px;border-radius:0 0 8px 8px;border-top:1px solid #30363d">'
        f'{footer}'
        f'</div></div>'
    )

    try:
        from app.workers.notifications import _send_email
        # _send_email is a blocking smtplib call. Offload to a thread so
        # we don't stall the event loop while the SMTP handshake + send
        # completes for each recipient. Failures are caught per-message
        # so one bad recipient doesn't poison the whole batch.
        for r in recipients:
            try:
                await asyncio.to_thread(_send_email, sender, r, subject, html)
            except Exception as inner:
                logger.error(f"Guidance email to {r} failed: {inner}")
    except Exception as e:
        logger.error(f"Guidance email batch to {recipients} failed: {e}")


async def auto_queue_recent_withdrawals(db: AsyncSession, *, force: bool = False) -> dict:
    """
    Find students marked inactive in the recent window and push them
    into the withdrawal guidance queue.

    The automated path calls ``auto_queue_withdrawals(db, list)`` from
    inside the Clever import with a freshly-built list of transitions.
    This helper is the manual-trigger equivalent — it queries for
    students whose ``status`` was flipped to ``'inactive'`` in the
    last N days (same ``guidance.auto_queue_recent_days`` setting as
    the enrollment side) and routes them through the existing
    ``auto_queue_withdrawals`` path.

    Uses ``updated_at`` (not ``created_at``) because withdrawal is an
    UPDATE to an existing row, not an insert.

    ``force`` is retained for API compatibility but not consulted —
    queue insertion is always on; notification gating lives elsewhere.
    """
    from app.modules.settings.repository import get_setting_value

    days_str = await get_setting_value(db, "guidance", "auto_queue_recent_days") or "3"
    try:
        recent_days = max(1, int(days_str))
    except (ValueError, TypeError):
        recent_days = 3

    rows = (await db.execute(text("""
        SELECT rs.id, rs.school, rs.first_name, rs.last_name, rs.email
        FROM roster_snapshots rs
        WHERE rs.status = 'inactive'
          AND rs.updated_at >= NOW() - make_interval(days => :days)
          AND NOT EXISTS (
              SELECT 1 FROM guidance_queue gq
              WHERE gq.student_id = rs.id
                AND gq.status IN ('open', 'in_progress')
                AND gq.category = 'withdrawal'
          )
    """).bindparams(days=recent_days))).all()

    withdrawn = [
        {
            "id": r[0],
            "school": r[1] or "",
            "first_name": r[2] or "",
            "last_name": r[3] or "",
            "email": r[4] or "",
        }
        for r in rows
    ]

    result = await auto_queue_withdrawals(db, withdrawn, force=True)
    result["recent_days"] = recent_days
    result["scanned"] = len(withdrawn)
    return result
