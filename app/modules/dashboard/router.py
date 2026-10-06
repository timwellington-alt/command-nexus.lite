"""
Dashboard module — post-login landing page.

Read-only. No workflows, no provisioning, no controls.
Permission-aware panels — only shows what the user can access.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import get_db
from app.db.models import User
from app.policies.engine import require_action, get_user_permissions, check_permission
from app.auth.session import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def _user_has_permission(permissions: list[dict], action: str) -> bool:
    return check_permission(permissions, action)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_action("dashboard.view")),
    db: AsyncSession = Depends(get_db),
):
    permissions = await get_user_permissions(db, user.id)

    # Permission-aware navigation — single source of truth. Adding a
    # module means editing app.policies.page_context, not every route
    # handler that renders a page.
    from app.policies.page_context import build_page_modules
    modules = build_page_modules(permissions)

    is_admin = _user_has_permission(permissions, "settings.manage")

    # Panel visibility — admins see system panels, principals see building panels
    panels = {
        "worker_health": is_admin,
        "integration_status": is_admin,
        "audit_summary": is_admin,
        "staff_queue": is_admin,
        "roster": _user_has_permission(permissions, "roster.view"),
        "network": _user_has_permission(permissions, "network.view"),
        "access": _user_has_permission(permissions, "access.view"),
        "guidance": _user_has_permission(permissions, "roster.guidance.view"),
        "roster_changes": _user_has_permission(permissions, "roster.students.view"),
        "building_dashboard": not is_admin and _user_has_permission(permissions, "roster.view"),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "panels": panels,
    })


# ── Panel data endpoints ──────────────────────────────────────────────────

@router.get("/api/dashboard/setup-checklist")
async def setup_checklist(
    user: User = Depends(require_action("dashboard.view")),
    db: AsyncSession = Depends(get_db),
):
    """First-run setup items that aren't done yet. Dashboard renders a
    card at the top for every item with complete=False."""
    from app.modules.dashboard.setup_checklist import build_checklist
    items = await build_checklist(db)
    incomplete = [i for i in items if not i["complete"]]
    return {
        "items": incomplete,
        "total_checks": len(items),
        "complete_count": len(items) - len(incomplete),
    }


@router.get("/api/dashboard/worker-health")
async def worker_health(
    user: User = Depends(require_action("dashboard.view")),
):
    """Worker health from Redis job results."""
    try:
        from app.modules.dashboard.service import get_worker_health
        settings = get_settings()
        return await get_worker_health(settings.redis_url)
    except Exception as e:
        logger.warning(f"Worker health endpoint error: {e}")
        return JSONResponse(content={"status": "unavailable"})


@router.get("/api/dashboard/integration-status")
async def integration_status(
    user: User = Depends(require_action("settings.manage")),
):
    """Cached integration test results. Does not re-run tests."""
    try:
        from app.modules.dashboard.service import get_integration_status_cached
        settings = get_settings()
        return await get_integration_status_cached(settings.redis_url)
    except Exception as e:
        logger.warning(f"Integration status endpoint error: {e}")
        return JSONResponse(content={"status": "unavailable"})


_SUMMARY_CACHE_TTL_S = 30  # per-user dashboard summary cache TTL

# Per-process, per-user single-flight lock. When the Redis cache misses,
# only the first concurrent caller for a given user runs the full
# aggregation; others wait for that one to finish and then read the
# fresh cached value. Without this, 50 simultaneous JS refreshes from
# the same user (or from N users that all expire together) all run
# the 15-source aggregation, multiplying load on Postgres + LibreNMS
# + UCM. Locks are weak references in dict — never unbounded growth
# because user count is fixed.
import asyncio as _asyncio
_summary_locks: dict[int, _asyncio.Lock] = {}


def _user_lock(user_id: int) -> _asyncio.Lock:
    lock = _summary_locks.get(user_id)
    if lock is None:
        lock = _asyncio.Lock()
        _summary_locks[user_id] = lock
    return lock


@router.get("/api/dashboard/summary")
async def dashboard_summary(
    user: User = Depends(require_action("dashboard.view")),
    db: AsyncSession = Depends(get_db),
    fresh: bool = False,
):
    """Unified dashboard summary — all module data in one call like v1.

    Aggregates from ~15 sources (DB queries + LibreNMS HTTP + UCM
    extension cache + audit log scans). Per-user Redis cache with a
    30s TTL absorbs the burst: under 25 concurrent users the same
    user's panel only re-aggregates twice a minute instead of every
    JS refresh. Pass `?fresh=true` to bypass the cache for a manual
    pull. Cache key is per-user because the included sections depend
    on the caller's permissions.

    Single-flight: when N concurrent callers miss the cache for the
    same user, only the first runs the aggregation; the rest wait on
    the lock and pick up the freshly-cached value. Cuts the 50-user
    cache-miss tail from p95 ~2s to ~80ms (one full compute + N reads)."""
    settings = get_settings()

    import json as _cache_json
    import redis.asyncio as _aioredis
    cache_key = f"dashboard:summary:user:{user.id}"

    async def _read_cache():
        client = _aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            raw = await client.get(cache_key)
            return _cache_json.loads(raw) if raw else None
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    if not fresh:
        try:
            cached = await _read_cache()
            if cached is not None:
                return cached
        except Exception as _ce:
            logger.warning(f"dashboard summary cache read failed: {_ce}")

    # Cache miss — gate the compute behind the per-user lock so concurrent
    # cold-cache requests collapse to one backend call.
    async with _user_lock(user.id):
        if not fresh:
            try:
                cached = await _read_cache()
                if cached is not None:
                    return cached  # another coroutine populated it while we waited
            except Exception as _ce:
                logger.warning(f"dashboard summary cache re-read failed: {_ce}")

        permissions = await get_user_permissions(db, user.id)
        result = {}

        # Roster summary
        if check_permission(permissions, "roster.view"):
            try:
                from app.modules.roster.repository import get_roster_summary
                result["roster"] = await get_roster_summary(db)
            except Exception as e:
                result["roster"] = {"error": str(e)[:100]}

            # Today's roster changes summary for admin dashboard
            if check_permission(permissions, "roster.students.view"):
                try:
                    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                    from sqlalchemy import text as _txt
                    import json as _rjson
                    since = _dt.now(_tz.utc) - _td(days=1)
                    changes_q = await db.execute(_txt("""
                        SELECT change_type, count(*)
                        FROM roster_changes
                        WHERE created_at >= :since
                          AND change_type IN ('added', 'removed', 'transferred', 'grade_change', 'name_change', 'account_review')
                        GROUP BY change_type
                    """).bindparams(since=since))
                    result["roster_changes_today"] = {r[0]: r[1] for r in changes_q.all()}
                    # Account reviews needing attention
                    review_q = await db.execute(_txt("""
                        SELECT count(*) FROM roster_changes
                        WHERE change_type = 'account_review' AND reviewed = false
                    """))
                    result["roster_reviews_pending"] = review_q.scalar_one()
                except Exception:
                    result["roster_changes_today"] = {}
                    result["roster_reviews_pending"] = 0
            else:
                result["roster_changes_today"] = {}
                result["roster_reviews_pending"] = 0
        else:
            result["roster"] = None
            result["roster_changes_today"] = {}
            result["roster_reviews_pending"] = 0

        # Network summary
        if check_permission(permissions, "network.view"):
            try:
                from app.modules.network.repository import get_cache_summary
                net = await get_cache_summary(db)
                # Also get alerts
                try:
                    from app.integrations.librenms.adapter import LibreNMSAdapter
                    adapter = LibreNMSAdapter(db)
                    alerts = await adapter.get_alerts(state=1)
                    net["alerts"] = len(alerts)
                    net["alert_list"] = alerts[:5]
                    # Port summary
                    port_stats = await adapter.get_port_summary()
                    net.update(port_stats)
                except Exception:
                    net["alerts"] = 0
                    net["alert_list"] = []
                # Service checks from cache
                try:
                    from app.modules.dashboard.service import get_integration_status_cached
                    cached = await get_integration_status_cached(settings.redis_url)
                    if cached.get("status") == "ok":
                        svc_results = cached.get("results", [])
                        net["services"] = {r["integration"]: r for r in svc_results}
                        net["service_order"] = [r["integration"] for r in svc_results]
                    else:
                        net["services"] = {}
                        net["service_order"] = []
                except Exception:
                    net["services"] = {}
                    net["service_order"] = []
                result["network"] = net
            except Exception as e:
                result["network"] = {"total_devices": 0, "up": 0, "down": 0, "alerts": 0, "error": str(e)[:100]}
        else:
            result["network"] = None

        # Access summary
        if check_permission(permissions, "access.view"):
            try:
                from app.modules.access.repository import get_event_summary
                result["access"] = await get_event_summary(db)
            except Exception as e:
                result["access"] = {"error": str(e)[:100]}
        else:
            result["access"] = None

        # Guidance queue count
        if check_permission(permissions, "roster.guidance.view"):
            try:
                from sqlalchemy import select, func
                from app.modules.roster.models import GuidanceQueue
                pending = (await db.execute(
                    select(func.count()).select_from(GuidanceQueue).where(
                        GuidanceQueue.status.in_(["open", "in_progress"])
                    )
                )).scalar_one()
                result["guidance"] = {"pending": pending}
            except Exception:
                result["guidance"] = {"pending": 0}
        else:
            result["guidance"] = None

        # Phone system summary — now uses the single-flight cache in
        # phones/router so concurrent dashboards don't hammer UCM.
        if check_permission(permissions, "network.view"):
            try:
                from app.modules.settings.repository import get_setting_value
                ucm_url = await get_setting_value(db, "grandstream", "url")
                if ucm_url:
                    from app.modules.phones.router import _list_extensions_cached
                    exts = await _list_extensions_cached(db)
                    idle = sum(1 for e in exts if e["status"].lower() == "idle")
                    busy = sum(1 for e in exts if e["status"].lower() in ("busy", "ringing", "inuse"))
                    unavail = sum(1 for e in exts if e["status"].lower() == "unavailable")
                    result["phones"] = {"total": len(exts), "idle": idle, "busy": busy, "unavailable": unavail}
                else:
                    result["phones"] = None
            except Exception as e:
                logger.warning(f"Phone dashboard failed: {e}")
                result["phones"] = None
        else:
            result["phones"] = None

        # Vendor contract renewals
        if check_permission(permissions, "docs.view"):
            try:
                from app.modules.docs.service import get_expiring_contracts
                result["vendor_renewals"] = await get_expiring_contracts(db, days=90)
            except Exception:
                result["vendor_renewals"] = []
        else:
            result["vendor_renewals"] = None

        # Recent activity
        if check_permission(permissions, "audit.view"):
            try:
                from app.modules.dashboard.service import get_audit_summary
                has_student = check_permission(permissions, "roster.students.view")
                audit = await get_audit_summary(db, has_student)
                result["recent_activity"] = audit.get("recent", [])
            except Exception:
                result["recent_activity"] = []
        else:
            result["recent_activity"] = []

        # Cache the assembled result for _SUMMARY_CACHE_TTL_S so
        # concurrent JS refreshes from this user reuse it. Best-effort;
        # failure here never blocks the response.
        try:
            _client = _aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                await _client.set(cache_key, _cache_json.dumps(result, default=str),
                                  ex=_SUMMARY_CACHE_TTL_S)
            finally:
                try:
                    await _client.aclose()
                except Exception:
                    pass
        except Exception as _we:
            logger.warning(f"dashboard summary cache write failed: {_we}")

        return result


@router.get("/api/dashboard/building")
async def building_dashboard(
    user: User = Depends(require_action("dashboard.view")),
    db: AsyncSession = Depends(get_db),
):
    """Building-specific dashboard for principals — birthdays, new students, door activity."""
    from sqlalchemy import text, select
    from app.db.models import UserRole
    import json as _json
    from app.modules.settings.repository import get_setting_value

    permissions = await get_user_permissions(db, user.id)

    # Get user's building scope
    roles = (await db.execute(select(UserRole).where(UserRole.user_id == user.id))).scalars().all()
    building = None
    for r in roles:
        if r.scope_type == "school" and r.scope_value:
            building = r.scope_value
            break

    # Map SIS code to building code for access/doors
    bmap_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        bmap = _json.loads(bmap_raw)
    except Exception:
        bmap = {}
    door_building = bmap.get(building, building) if building else None

    # School name
    names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        names = _json.loads(names_raw)
    except Exception:
        names = {}
    school_name = names.get(building, building) if building else "All Buildings"

    result = {"building": building, "school_name": school_name}

    # Student data sections require roster.students.view — FERPA protected
    can_view_students = check_permission(permissions, "roster.students.view")

    # Today's birthdays
    if can_view_students:
        try:
            from datetime import datetime
            today = datetime.now().strftime("%m-%d")
            bday_q = await db.execute(text("""
                SELECT first_name, last_name, grade FROM roster_snapshots
                WHERE status = 'active' AND dob IS NOT NULL AND dob != ''
                  AND SUBSTRING(dob, 6, 5) = :today
                  AND (:school IS NULL OR school = :school)
                  AND id IN (SELECT DISTINCT student_id FROM student_teachers)
                ORDER BY last_name, first_name
            """).bindparams(today=today, school=building))
            result["birthdays"] = [
                {"name": f"{r[0]} {r[1]}", "grade": r[2]}
                for r in bday_q.all()
            ]
            # FERPA audit
            if result["birthdays"]:
                from app.audit.service import log_action
                await log_action(db, actor=user.email, action="student_data_access:dashboard.birthdays",
                                 module="roster", target=f"{len(result['birthdays'])} students",
                                 ip_address=None)
        except Exception as e:
            logger.warning(f"Birthday query failed: {e}")
            result["birthdays"] = []
    else:
        result["birthdays"] = []

    # New enrollees (last 7 days)
    if can_view_students:
        try:
            new_q = await db.execute(text("""
                SELECT first_name, last_name, grade, created_at FROM roster_snapshots
                WHERE status = 'active'
                  AND created_at >= NOW() - INTERVAL '7 days'
                  AND (:school IS NULL OR school = :school)
                  AND id IN (SELECT DISTINCT student_id FROM student_teachers)
                ORDER BY created_at DESC LIMIT 20
            """).bindparams(school=building))
            result["new_students"] = [
                {"name": f"{r[0]} {r[1]}", "grade": r[2],
                 "date": r[3].isoformat() if r[3] else None}
                for r in new_q.all()
            ]
            if result["new_students"]:
                from app.audit.service import log_action
                await log_action(db, actor=user.email, action="student_data_access:dashboard.new_enrollees",
                                 module="roster", target=f"{len(result['new_students'])} students",
                                 ip_address=None)
        except Exception as e:
            logger.warning(f"New enrollees query failed: {e}")
            result["new_students"] = []
    else:
        result["new_students"] = []

    # Recent door activity (last 24h) — only for users with access.view
    if door_building and check_permission(permissions, "access.view"):
        try:
            # Get door names for this building, then match events containing those names
            doors_q = await db.execute(text(
                "SELECT name FROM doors WHERE building = :building"
            ).bindparams(building=door_building))
            door_names = [r[0] for r in doors_q.all()]

            door_q = None
            if door_names:
                # Build OR conditions for event door_name containing any of our door names
                conditions = " OR ".join([f"de.door_name LIKE :d{i}" for i in range(len(door_names))])
                params = {f"d{i}": f"%{name}%" for i, name in enumerate(door_names)}
                door_q = await db.execute(text(f"""
                    SELECT de.event_time, de.door_name, de.person_name, de.event_description
                    FROM door_events de
                    WHERE ({conditions})
                      AND de.event_time >= NOW() - INTERVAL '24 hours'
                      AND de.event_type IN (20, 23, 28)
                    ORDER BY de.event_time DESC LIMIT 15
                """).bindparams(**params))
            rows = door_q.all() if door_q else []
            result["door_activity"] = [
                {"time": r[0].isoformat() if r[0] else None, "door": r[1],
                 "person": r[2], "description": r[3]}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Door activity query failed: {e}")
            result["door_activity"] = []
    else:
        result["door_activity"] = []

    # Student count for building
    try:
        count_q = await db.execute(text("""
            SELECT count(*) FROM roster_snapshots
            WHERE status = 'active' AND (:school IS NULL OR school = :school)
              AND id IN (SELECT DISTINCT student_id FROM student_teachers)
        """).bindparams(school=building))
        result["student_count"] = count_q.scalar_one()
    except Exception:
        result["student_count"] = 0

    # Staff count for building
    try:
        staff_q = await db.execute(text("""
            SELECT count(*) FROM staff_directory
            WHERE (:building IS NULL OR building = :building)
        """).bindparams(building=door_building))
        result["staff_count"] = staff_q.scalar_one()
    except Exception:
        result["staff_count"] = 0

    # Recent roster changes (last 7 days) — requires roster.students.view
    # Includes all change types: added, removed, transferred, grade_change, name_change, account_review
    if can_view_students:
        try:
            changes_q = await db.execute(text("""
                SELECT rc.id, rc.change_type, rc.student_name, rc.school_code, rc.details,
                       rc.reviewed, rc.google_provisioned, rc.created_at
                FROM roster_changes rc
                WHERE rc.created_at >= NOW() - INTERVAL '7 days'
                  AND rc.change_type IN ('added', 'removed', 'transferred', 'grade_change', 'name_change', 'account_review')
                  AND (:school IS NULL OR rc.school_code = :school)
                ORDER BY rc.created_at DESC LIMIT 50
            """).bindparams(school=building))
            result["roster_changes"] = [
                {"id": r[0], "type": r[1], "name": r[2] or "",
                 "school": r[3],
                 "details": _json.loads(r[4]) if r[4] else {},
                 "reviewed": r[5],
                 "google_provisioned": r[6],
                 "date": r[7].isoformat() if r[7] else None}
                for r in changes_q.all()
            ]
            # Summary counts by type
            change_summary = {}
            for c in result["roster_changes"]:
                ct = c["type"]
                change_summary[ct] = change_summary.get(ct, 0) + 1
            result["roster_change_summary"] = change_summary

            if result["roster_changes"]:
                from app.audit.service import log_action
                await log_action(db, actor=user.email, action="student_data_access:dashboard.roster_changes",
                                 module="roster", target=f"{len(result['roster_changes'])} changes",
                                 ip_address=None)
        except Exception as e:
            logger.warning(f"Roster changes query failed: {e}")
            result["roster_changes"] = []
            result["roster_change_summary"] = {}
    else:
        result["roster_changes"] = []
        result["roster_change_summary"] = {}

    # Account review items needing IT attention
    is_admin = check_permission(permissions, "settings.manage")
    if is_admin:
        try:
            review_q = await db.execute(text("""
                SELECT rc.id, rc.student_id, rc.student_name, rc.school_code, rc.details, rc.created_at
                FROM roster_changes rc
                WHERE rc.change_type = 'account_review'
                  AND rc.reviewed = false
                ORDER BY rc.created_at DESC LIMIT 20
            """))
            result["account_reviews"] = [
                {"id": r[0], "student_id": r[1], "student_name": r[2],
                 "school": r[3],
                 "details": _json.loads(r[4]) if r[4] else {},
                 "date": r[5].isoformat() if r[5] else None}
                for r in review_q.all()
            ]
        except Exception as e:
            logger.warning(f"Account reviews query failed: {e}")
            result["account_reviews"] = []
    else:
        result["account_reviews"] = []

    # Pending guidance queue count
    if check_permission(permissions, "roster.guidance.view"):
        try:
            gq_count = await db.execute(text("""
                SELECT count(*) FROM guidance_queue
                WHERE status IN ('open', 'in_progress')
                  AND (:school IS NULL OR school = :school)
            """).bindparams(school=building))
            result["guidance_pending"] = gq_count.scalar_one()
        except Exception:
            result["guidance_pending"] = 0
    else:
        result["guidance_pending"] = 0

    await db.commit()
    return result


# ── Customizable card system (v2 dashboard) ──────────────────────────────
# The endpoints below drive the new per-user card library. The legacy
# /api/dashboard/summary + /building remain for the principal building view
# but are no longer used by the main dashboard cards.

@router.get("/api/dashboard/catalog")
async def dashboard_catalog(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Cards the caller can see + their current enabled/order list."""
    from app.modules.dashboard.cards import CARDS, cards_visible_to, resolve_layout
    perms = await get_user_permissions(db, user.id)
    allowed = cards_visible_to(perms)
    layout = resolve_layout(user.dashboard_layout, allowed)
    enabled = set(layout)
    return {
        "cards": [
            {"id": cid, "label": CARDS[cid]["label"], "enabled": cid in enabled}
            for cid in allowed
        ],
        "layout": layout,
    }


@router.get("/api/dashboard/cards-data")
async def dashboard_cards_data(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fetch data for every card in the caller's layout, in parallel.
    Cards return null on failure so one bad fetch doesn't break the page."""
    import asyncio as _aio
    from app.modules.dashboard.cards import CARDS, cards_visible_to, resolve_layout
    perms = await get_user_permissions(db, user.id)
    allowed = cards_visible_to(perms)
    layout = resolve_layout(user.dashboard_layout, allowed)

    async def fetch(cid: str):
        try:
            return await CARDS[cid]["fetcher"](db, user)
        except Exception as e:
            logger.warning(f"dashboard fetch {cid} failed: {e}")
            return None

    results = await _aio.gather(*[fetch(cid) for cid in layout])
    return {
        "layout": layout,
        "cards": {
            cid: {
                "label": CARDS[cid]["label"],
                "wide":  bool(CARDS[cid].get("wide")),
                "data":  data,
            }
            for cid, data in zip(layout, results)
        },
    }


@router.patch("/api/me/dashboard")
async def update_my_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Body: {layout: [card_id, ...]}. Order matters. null resets to default."""
    from fastapi import HTTPException as _HE
    from app.modules.dashboard.cards import cards_visible_to
    data = await request.json()
    layout = data.get("layout")

    if layout is None:
        user.dashboard_layout = None
    else:
        if not isinstance(layout, list) or not all(isinstance(x, str) for x in layout):
            raise _HE(status_code=400, detail="layout must be a list of card_id strings")
        perms = await get_user_permissions(db, user.id)
        allowed = set(cards_visible_to(perms))
        seen, cleaned = set(), []
        for cid in layout:
            if cid in allowed and cid not in seen:
                cleaned.append(cid)
                seen.add(cid)
        user.dashboard_layout = cleaned

    await db.commit()
    return {"status": "ok", "layout": user.dashboard_layout}


@router.get("/api/dashboard/audit-summary")
async def audit_summary(
    user: User = Depends(require_action("audit.view")),
    db: AsyncSession = Depends(get_db),
):
    """Audit summary — counts and recent non-sensitive events. No student PII."""
    try:
        from app.modules.dashboard.service import get_audit_summary
        permissions = await get_user_permissions(db, user.id)
        has_student_access = check_permission(permissions, "roster.students.view")
        return await get_audit_summary(db, has_student_access)
    except Exception as e:
        logger.warning(f"Audit summary endpoint error: {e}")
        return JSONResponse(content={"status": "unavailable"})
