"""Roster Analytics dashboard — /roster/analytics + per-section JSON APIs.

Six sections, all gated on roster.analytics.view:

  enrollment    Inflow/outflow bar + net-enrolled line
  grade         Grade headcount grouped bar (this year vs YoY where data allows)
  google        Google account lifecycle stacked area + provision/archive SLA distributions
  capacity      IT capacity: Chromebooks-per-building, avg age, repair rate
  engagement    Placeholder for login rate — no local cache yet (see plan)
  anomaly       Recent import anomalies + per-building sparklines

Data comes from roster_analytics_daily + roster_analytics_sla_events
(populated nightly by snapshot_roster_analytics). Live queries hit
roster_snapshots / roster_imports for supplementary context that
doesn't need a rollup.

YoY comparisons degrade gracefully: chart returns nulls for months
that lack a prior-year data point.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action
from app.db.engine import get_db
from app.db.models import User
from app.policies.engine import get_user_permissions, require_action
from app.policies.page_context import build_page_modules

logger = logging.getLogger(__name__)
router = APIRouter(tags=["roster", "analytics"])
templates = Jinja2Templates(directory="app/templates")


DEFAULT_WINDOW_DAYS = 30


@router.get("/roster/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    latest_snapshot = (await db.execute(text(
        "SELECT MAX(snapshot_date) FROM roster_analytics_daily"
    ))).scalar()
    oldest_snapshot = (await db.execute(text(
        "SELECT MIN(snapshot_date) FROM roster_analytics_daily"
    ))).scalar()
    partial_yoy = None
    if oldest_snapshot:
        days = (date.today() - oldest_snapshot).days
        if days < 365:
            partial_yoy = oldest_snapshot.isoformat()

    from app.policies.engine import check_permission
    can_manage_recipients = check_permission(permissions, "roster.attendance_reports.manage")

    # Buildings list for the recipient dropdown — internal codes only
    buildings = [r[0] for r in (await db.execute(text("""
        SELECT DISTINCT building_code FROM roster_analytics_daily
        WHERE building_code <> '' ORDER BY building_code
    """))).all()]

    return templates.TemplateResponse("roster_analytics.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "latest_snapshot": latest_snapshot.isoformat() if latest_snapshot else None,
        "partial_yoy": partial_yoy,
        "can_manage_recipients": can_manage_recipients,
        "buildings_for_recipients": buildings,
    })


# ── Section endpoints ────────────────────────────────────────────────────


@router.get("/api/roster/analytics/enrollment")
async def enrollment_section(
    days: int = 30,
    building: str | None = None,
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    """Per-day added/removed/transferred + net enrolled line."""
    days = max(1, min(days, 365))
    start = date.today() - timedelta(days=days)

    params: dict = {"start": start}
    if building:
        # Building rollup row (grade='').
        where_bc = "AND building_code = :building AND grade = ''"
        params["building"] = building
    else:
        # District total row only — the ('','') row already sums buildings.
        where_bc = "AND building_code = '' AND grade = ''"

    rows = (await db.execute(text(f"""
        SELECT snapshot_date, added_today, removed_today,
               transferred_today, enrolled_count
        FROM roster_analytics_daily
        WHERE snapshot_date >= :start {where_bc}
        ORDER BY snapshot_date
    """).bindparams(**params))).mappings().all()

    return {
        "series": [
            {
                "date": r["snapshot_date"].isoformat(),
                "added": int(r["added_today"] or 0),
                "removed": int(r["removed_today"] or 0),
                "transferred": int(r["transferred_today"] or 0),
                "enrolled": int(r["enrolled_count"] or 0),
            }
            for r in rows
        ],
        "building": building,
    }


@router.get("/api/roster/analytics/grade")
async def grade_section(
    building: str | None = None,
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    """Grade headcount today + YoY same-date if data allows."""
    today = date.today()
    yoy = today - timedelta(days=365)

    params: dict = {"today": today, "yoy": yoy}
    where_bc = "AND building_code = ''"
    if building:
        where_bc = "AND building_code = :building"
        params["building"] = building

    rows = (await db.execute(text(f"""
        SELECT snapshot_date, grade, SUM(enrolled_count) AS enrolled
        FROM roster_analytics_daily
        WHERE snapshot_date IN (:today, :yoy)
          AND grade <> ''
          {where_bc}
        GROUP BY snapshot_date, grade
    """).bindparams(**params))).mappings().all()

    current: dict[str, int] = {}
    prior: dict[str, int] = {}
    for r in rows:
        target = current if r["snapshot_date"] == today else prior
        target[r["grade"]] = int(r["enrolled"] or 0)

    def _sort_key(g: str) -> tuple[int, str]:
        # Kindergarten and PK first, then numeric grades ascending.
        low = g.lower()
        if low in ("pk", "prek"): return (-2, g)
        if low in ("k", "kg"): return (-1, g)
        try:
            return (int(g), g)
        except ValueError:
            return (999, g)

    grades = sorted(set(current) | set(prior), key=_sort_key)
    return {
        "grades": grades,
        "current": [current.get(g, 0) for g in grades],
        "prior": [prior.get(g, None) for g in grades],
        "current_date": today.isoformat(),
        "prior_date": yoy.isoformat(),
        "yoy_available": bool(prior),
        "building": building,
    }


@router.get("/api/roster/analytics/google")
async def google_section(
    days: int = 30,
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    """Lifecycle trend + SLA distributions + license placeholder."""
    days = max(7, min(days, 180))
    start = date.today() - timedelta(days=days)

    trend = (await db.execute(text("""
        SELECT snapshot_date,
               google_active, google_suspended, google_archived, google_missing
        FROM roster_analytics_daily
        WHERE snapshot_date >= :start
          AND building_code = ''
          AND grade = ''
        ORDER BY snapshot_date
    """).bindparams(start=start))).mappings().all()

    def _bucket_seconds(s: int) -> str:
        if s is None:
            return "pending"
        h = s / 3600
        if h < 1: return "<1h"
        if h < 4: return "1-4h"
        if h < 24: return "4-24h"
        if h < 24 * 7: return "1-7d"
        return ">7d"

    # Provision-only. Archive SLA is intentionally not measured yet —
    # see the comment in roster_analytics_snapshot_job._backfill_sla_events
    # for why the write-path audit history isn't reliable enough today.
    sla_rows = (await db.execute(text("""
        SELECT sla_seconds
        FROM roster_analytics_sla_events
        WHERE sis_event_at >= NOW() - INTERVAL '90 days'
          AND event_type = 'provision'
    """))).mappings().all()

    order = ["<1h", "1-4h", "4-24h", "1-7d", ">7d", "pending"]
    provision_dist = {b: 0 for b in order}
    for r in sla_rows:
        provision_dist[_bucket_seconds(r["sla_seconds"])] += 1

    return {
        "trend": [
            {
                "date": r["snapshot_date"].isoformat(),
                "active": int(r["google_active"] or 0),
                "suspended": int(r["google_suspended"] or 0),
                "archived": int(r["google_archived"] or 0),
                "missing": int(r["google_missing"] or 0),
            }
            for r in trend
        ],
        "sla_buckets": order,
        "sla_provision": [provision_dist[b] for b in order],
        "license_placeholder": True,
    }


@router.get("/api/roster/analytics/capacity")
async def capacity_section(
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    """Chromebook capacity per building + fleet age + repair rate."""
    today = date.today()
    rows = (await db.execute(text("""
        SELECT building_code, enrolled_count, cb_assigned,
               cb_avg_age_days, cb_repairs_30d
        FROM roster_analytics_daily
        WHERE snapshot_date = :today
          AND grade = ''
          AND building_code <> ''
        ORDER BY building_code
    """).bindparams(today=today))).mappings().all()

    result = []
    for r in rows:
        enr = int(r["enrolled_count"] or 0)
        cb = int(r["cb_assigned"] or 0)
        rep = int(r["cb_repairs_30d"] or 0)
        # Repairs per 100 kids per month — normalized rate
        rep_per_100 = round(100.0 * rep / enr, 2) if enr else 0.0
        result.append({
            "building": r["building_code"],
            "enrolled": enr,
            "cb_assigned": cb,
            "coverage_pct": round(100.0 * cb / enr, 1) if enr else 0.0,
            "avg_age_days": r["cb_avg_age_days"],
            "repairs_30d": rep,
            "repairs_per_100": rep_per_100,
        })
    return {"buildings": result}


@router.get("/api/roster/analytics/engagement")
async def engagement_section(
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    """Placeholder: login-rate data not sampled locally yet.

    The rollup persists logged_in_7d / logged_in_30d columns pre-
    zeroed. A future weekly sampled Google Reports API pull will
    populate them; today's response signals unavailable so the UI
    renders a clearly-labeled placeholder card rather than a
    misleading 0.
    """
    return {"available": False, "reason": "login_cache_not_implemented"}


@router.get("/api/roster/analytics/anomaly")
async def anomaly_section(
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    """Recent import anomalies + per-building 14-day enrollment sparklines."""
    # Recent import anomalies from roster_imports.notes / status
    imports = (await db.execute(text("""
        SELECT id, started_at, filename, status, added,
               removed, errors, error_detail
        FROM roster_imports
        ORDER BY started_at DESC
        LIMIT 20
    """))).mappings().all()

    sparklines = (await db.execute(text("""
        SELECT snapshot_date, building_code, enrolled_count
        FROM roster_analytics_daily
        WHERE snapshot_date >= :start
          AND grade = ''
          AND building_code <> ''
        ORDER BY building_code, snapshot_date
    """).bindparams(start=date.today() - timedelta(days=14)))).mappings().all()

    per_building: dict[str, list[dict]] = {}
    for r in sparklines:
        per_building.setdefault(r["building_code"], []).append({
            "date": r["snapshot_date"].isoformat(),
            "enrolled": int(r["enrolled_count"] or 0),
        })

    return {
        "recent_imports": [
            {
                "id": r["id"],
                "at": r["started_at"].isoformat() if r["started_at"] else None,
                "source": r["filename"] or "",
                "status": r["status"],
                "added": r["added"],
                "removed": r["removed"],
                "errors": r["errors"],
                "notes": (r["error_detail"] or "")[:200],
            }
            for r in imports
        ],
        "sparklines": [
            {"building": bc, "points": pts}
            for bc, pts in sorted(per_building.items())
        ],
    }


@router.get("/api/roster/analytics/attendance")
async def attendance_section(
    days: int = 30,
    building: str | None = None,
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    """Attendance dashboard section — sources Phase C tables."""
    days = max(7, min(days, 365))
    start = date.today() - timedelta(days=days)

    trend_params: dict = {"start": start}
    trend_where = ""
    if building:
        trend_where = "AND school_code = :building"
        trend_params["building"] = building

    trend = (await db.execute(text(f"""
        SELECT calendar_date, school_code,
               total_absences, distinct_students_absent,
               enrolled_count, absence_rate_pct
        FROM attendance_daily_stats
        WHERE calendar_date >= :start {trend_where}
        ORDER BY school_code, calendar_date
    """).bindparams(**trend_params))).mappings().all()

    # Group into per-school series
    per_school: dict[str, list[dict]] = {}
    for r in trend:
        per_school.setdefault(r["school_code"], []).append({
            "date": r["calendar_date"].isoformat(),
            "rate": float(r["absence_rate_pct"] or 0),
            "absences": int(r["total_absences"] or 0),
            "students": int(r["distinct_students_absent"] or 0),
        })

    # Summary numbers
    sum_params: dict = {}
    sum_where = ""
    if building:
        sum_where = "WHERE school_code = :building"
        sum_params["building"] = building
    summary = (await db.execute(text(f"""
        SELECT
            COUNT(*) AS total_students,
            COUNT(*) FILTER (WHERE chronic_absent) AS chronic_count,
            ROUND(AVG(absence_rate_pct), 2) AS avg_rate,
            MAX(computed_at) AS last_computed
        FROM student_analytics
        {sum_where}
    """).bindparams(**sum_params))).mappings().first()

    # Chronic student list — join roster_snapshots for name
    chronic_params: dict = {}
    chronic_where = "WHERE sa.chronic_absent"
    if building:
        chronic_where += " AND sa.school_code = :building"
        chronic_params["building"] = building
    chronic = (await db.execute(text(f"""
        SELECT sa.sis_id, sa.school_code, sa.grade,
               sa.days_absent_ytd, sa.days_enrolled_ytd,
               sa.absence_rate_pct,
               rs.first_name, rs.last_name
        FROM student_analytics sa
        LEFT JOIN roster_snapshots rs ON rs.sis_id = sa.sis_id
        {chronic_where}
        ORDER BY sa.absence_rate_pct DESC, sa.days_absent_ytd DESC
        LIMIT 200
    """).bindparams(**chronic_params))).mappings().all()

    return {
        "summary": {
            "total_students": int(summary["total_students"] or 0),
            "chronic_count": int(summary["chronic_count"] or 0),
            "avg_rate_pct": float(summary["avg_rate"] or 0),
            "last_computed": summary["last_computed"].isoformat() if summary["last_computed"] else None,
        },
        "trend_by_school": [
            {"school": sc, "points": pts}
            for sc, pts in sorted(per_school.items())
        ],
        "chronic": [
            {
                "sis_id": r["sis_id"],
                "name": f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "—",
                "school": r["school_code"] or "",
                "grade": r["grade"] or "",
                "absent": int(r["days_absent_ytd"] or 0),
                "enrolled": int(r["days_enrolled_ytd"] or 0),
                "rate": float(r["absence_rate_pct"] or 0),
            }
            for r in chronic
        ],
        "building": building,
    }


@router.get("/settings/attendance-recipients", response_class=HTMLResponse)
async def attendance_recipients_page(
    request: Request,
    user: User = Depends(require_action("roster.attendance_reports.manage")),
    db: AsyncSession = Depends(get_db),
):
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)

    buildings = [r[0] for r in (await db.execute(text("""
        SELECT DISTINCT building_code FROM roster_analytics_daily
        WHERE building_code <> '' ORDER BY building_code
    """))).all()]

    # Full-name labels for the dropdown — Tim's ask, so operators
    # aren't picking blind three-letter codes.
    import json as _json
    from app.modules.settings.buildings import get_building_maps
    from app.modules.settings.repository import get_setting_value
    bmap = await get_building_maps(db)
    sis_to_internal = bmap.get("sis_to_internal", {})
    names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        names_map = _json.loads(names_raw)
    except Exception:
        names_map = {}
    internal_to_full: dict[str, str] = {}
    for sis, internal in sis_to_internal.items():
        full = names_map.get(sis)
        if full:
            internal_to_full[internal] = full

    buildings_with_names = [
        {"code": b, "full_name": internal_to_full.get(b, b)} for b in buildings
    ]

    return templates.TemplateResponse("roster_attendance_recipients.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "buildings": buildings_with_names,
    })


@router.get("/api/roster/analytics/attendance-schedule")
async def get_attendance_schedule(
    user: User = Depends(require_action("roster.attendance_reports.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.settings.repository import get_setting_value
    hour = await get_setting_value(db, "attendance", "reports_send_hour") or "11"
    minute = await get_setting_value(db, "attendance", "reports_send_minute") or "0"
    weekdays = await get_setting_value(db, "attendance", "reports_weekdays") or "0,1,2,3,4"
    tz = await get_setting_value(db, "branding", "timezone") or "America/New_York"
    try:
        hour_int = int(hour)
    except ValueError:
        hour_int = 11
    try:
        minute_int = int(minute)
    except ValueError:
        minute_int = 0
    # Snap to the 15-min grid the gate uses, so a bad save is visible
    # in the UI and doesn't silently miss its window.
    minute_int = (minute_int // 15) * 15
    return {
        "send_hour": hour_int,
        "send_minute": minute_int,
        "weekdays": weekdays,
        "timezone": tz,
    }


@router.post("/api/roster/analytics/attendance-schedule")
async def set_attendance_schedule(
    body: dict,
    user: User = Depends(require_action("roster.attendance_reports.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.settings.repository import upsert_integration_setting
    hour = body.get("send_hour")
    minute = body.get("send_minute", 0)
    if hour is None or not isinstance(hour, int) or not (0 <= hour <= 23):
        raise HTTPException(400, "send_hour must be an integer 0-23")
    if not isinstance(minute, int) or minute not in (0, 15, 30, 45):
        raise HTTPException(400, "send_minute must be 0, 15, 30, or 45")
    await upsert_integration_setting(
        db, integration="attendance", key="reports_send_hour",
        value=str(hour), updated_by=user.email,
    )
    await upsert_integration_setting(
        db, integration="attendance", key="reports_send_minute",
        value=str(minute), updated_by=user.email,
    )
    await log_action(
        db, actor=user.email, action="roster.attendance_reports.schedule_updated",
        module="roster", target="attendance.reports_send_time",
        details=json.dumps({"send_hour": hour, "send_minute": minute}),
    )
    await db.commit()
    return {"ok": True, "send_hour": hour, "send_minute": minute}


@router.get("/api/roster/analytics/attendance-recipients")
async def list_recipients(
    user: User = Depends(require_action("roster.attendance_reports.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.roster import attendance_reports as ar
    return {"recipients": await ar.get_recipients(db)}


@router.post("/api/roster/analytics/attendance-recipients")
async def add_recipient(
    body: dict,
    user: User = Depends(require_action("roster.attendance_reports.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.roster import attendance_reports as ar
    email = (body.get("email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    building = (body.get("building_code") or "").strip()
    notes = (body.get("notes") or "").strip() or None
    rid = await ar.add_recipient(
        db, building_code=building, email=email, notes=notes, actor=user.email,
    )
    await log_action(
        db, actor=user.email, action="roster.attendance_reports.recipient_added",
        module="roster", target=email,
        details=json.dumps({"building_code": building, "notes": notes, "recipient_id": rid}),
    )
    await db.commit()
    return {"id": rid, "ok": True}


@router.post("/api/roster/analytics/attendance-recipients/{rid}/toggle")
async def toggle_recipient(
    rid: int, body: dict,
    user: User = Depends(require_action("roster.attendance_reports.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.roster import attendance_reports as ar
    active = bool(body.get("active", True))
    ok = await ar.set_active(db, rid, active)
    if not ok:
        raise HTTPException(404, "Recipient not found")
    await log_action(
        db, actor=user.email, action="roster.attendance_reports.recipient_toggled",
        module="roster", target=str(rid),
        details=json.dumps({"active": active}),
    )
    await db.commit()
    return {"ok": True}


@router.delete("/api/roster/analytics/attendance-recipients/{rid}")
async def delete_recipient(
    rid: int,
    user: User = Depends(require_action("roster.attendance_reports.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.roster import attendance_reports as ar
    ok = await ar.delete_recipient(db, rid)
    if not ok:
        raise HTTPException(404, "Recipient not found")
    await log_action(
        db, actor=user.email, action="roster.attendance_reports.recipient_deleted",
        module="roster", target=str(rid), details=None,
    )
    await db.commit()
    return {"ok": True}


@router.post("/api/roster/analytics/attendance-recipients/send-test")
async def send_test_digest(
    body: dict,
    user: User = Depends(require_action("roster.attendance_reports.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Send today's digest to one specific address, on-demand.

    Useful for verifying content + delivery without waiting for the
    nightly job.
    """
    from app.modules.roster import attendance_reports as ar
    from app.modules.settings.repository import get_setting_value

    to = (body.get("email") or "").strip()
    if not to or "@" not in to:
        raise HTTPException(400, "Valid email required")
    building = (body.get("building_code") or "").strip()

    digest = await ar.build_building_digest(
        db, building_code=building, report_date=date.today(),
    )
    if digest is None:
        return {"ok": False, "reason": "no data to report"}

    sender = (await get_setting_value(db, "attendance", "report_sender_email")
              or ar.DEFAULT_SENDER)
    dashboard = (await get_setting_value(db, "branding", "public_base_url")
                 or "http://localhost:8000") + "/roster/analytics"
    html = ar.render_digest_html(digest, dashboard)
    subject = f"[Nexus] Absentees — {digest['display_name']} — {digest['report_date'].isoformat()}"

    try:
        msg_id = await ar.send_digest_email(sender, to, subject, html)
    except Exception as e:
        logger.exception("send_test_digest failed")
        raise HTTPException(500, f"Send failed: {e}")

    await log_action(
        db, actor=user.email, action="roster.attendance_reports.test_sent",
        module="roster", target=to,
        details=json.dumps({"building_code": building, "gmail_message_id": msg_id}),
    )
    await db.commit()
    return {"ok": True, "gmail_message_id": msg_id}


@router.get("/api/roster/analytics/buildings")
async def building_list(
    user: User = Depends(require_action("roster.analytics.view")),
    db: AsyncSession = Depends(get_db),
):
    """Available building filter values from the latest snapshot."""
    rows = (await db.execute(text("""
        SELECT DISTINCT building_code
        FROM roster_analytics_daily
        WHERE building_code <> ''
        ORDER BY building_code
    """))).all()
    return {"buildings": [r[0] for r in rows]}
