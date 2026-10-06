"""
Audit and Reporting module — user-facing audit log surface.

Provides filterable, paginated access to the audit trail.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import AuditLog, User
from app.policies.engine import require_action, get_user_permissions, check_permission
from app.policies.page_context import build_page_modules

logger = logging.getLogger(__name__)
router = APIRouter(tags=["audit"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: User = Depends(require_action("audit.view")),
    db: AsyncSession = Depends(get_db),
):
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    return templates.TemplateResponse("audit.html", {
        "request": request,
        "user": user,
        "modules": modules,
    })


@router.get("/api/audit/log")
async def audit_log(
    module: str = Query(None),
    action: str = Query(None),
    actor: str = Query(None),
    outcome: str = Query(None),
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_action("audit.view")),
    db: AsyncSession = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = (
        select(AuditLog)
        .where(AuditLog.created_at >= cutoff)
        .order_by(desc(AuditLog.created_at))
    )

    if module and module != "all":
        q = q.where(AuditLog.module == module)
    if action and action != "all":
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if actor:
        q = q.where(AuditLog.actor.ilike(f"%{actor}%"))
    if outcome and outcome != "all":
        q = q.where(AuditLog.outcome == outcome)

    # Count total matching
    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    # Paginate
    q = q.offset(offset).limit(limit)
    result = await db.execute(q)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "entries": [
            {
                "id": log.id,
                "actor": log.actor,
                "action": log.action,
                "target": log.target,
                "module": log.module,
                "outcome": log.outcome,
                "details": log.details,
                "ip_address": log.ip_address,
                "correlation_id": log.correlation_id,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in result.scalars().all()
        ],
    }


@router.get("/api/audit/modules")
async def audit_modules(
    user: User = Depends(require_action("audit.view")),
    db: AsyncSession = Depends(get_db),
):
    """Distinct module names for filter dropdown."""
    result = await db.execute(
        select(AuditLog.module).distinct().order_by(AuditLog.module)
    )
    return [r[0] for r in result.all()]


@router.get("/api/audit/actions")
async def audit_actions(
    user: User = Depends(require_action("audit.view")),
    db: AsyncSession = Depends(get_db),
):
    """Distinct action types for filter dropdown."""
    result = await db.execute(
        select(AuditLog.action).distinct().order_by(AuditLog.action)
    )
    return [r[0] for r in result.all()]


@router.get("/api/audit/student-access")
async def student_access_log(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(200, ge=1, le=1000),
    user: User = Depends(require_action("audit.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    FERPA access log — all student data access events.
    Queries actions matching 'student_data_*' pattern.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = (
        select(AuditLog)
        .where(
            AuditLog.created_at >= cutoff,
            AuditLog.action.like("student_data_%"),
        )
        .order_by(desc(AuditLog.created_at))
        .limit(limit)
    )
    result = await db.execute(q)

    return {
        "entries": [
            {
                "id": log.id,
                "actor": log.actor,
                "action": log.action,
                "target": log.target,
                "module": log.module,
                "details": log.details,
                "ip_address": log.ip_address,
                "correlation_id": log.correlation_id,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in result.scalars().all()
        ],
    }
