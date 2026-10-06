"""Roster (student) exports catalog page + per-export download endpoint.

Mirror of app/modules/staff/exports_router.py — see that file for the
rationale on filename patterns, streaming, and audit logging.
"""
import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action
from app.db.engine import get_db
from app.db.models import User
from app.modules.roster import exports as roster_exports
from app.policies.engine import get_user_permissions, require_action
from app.policies.page_context import build_page_modules


router = APIRouter(tags=["roster", "exports"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/roster/exports", response_class=HTMLResponse)
async def roster_exports_page(
    request: Request,
    user: User = Depends(require_action("roster.students.view")),
    db: AsyncSession = Depends(get_db),
):
    """Catalog of downloadable student-data exports."""
    await roster_exports.ensure_discovered(db)
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    return templates.TemplateResponse("roster_exports.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "exporters": roster_exports.get_all(),
    })


@router.get("/api/roster/exports/{exporter_id}/download")
async def download_roster_export(
    exporter_id: str,
    request: Request,
    user: User = Depends(require_action("roster.students.view")),
    db: AsyncSession = Depends(get_db),
):
    await roster_exports.ensure_discovered(db)
    exp = roster_exports.get_by_id(exporter_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Unknown exporter")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    row_count = 0
    async for row in exp.generator(db):
        writer.writerow(row)
        row_count += 1

    filename = exp.filename_pattern.format(
        yyyymmdd=datetime.now().strftime("%Y%m%d"),
        yyyy_mm_dd=datetime.now().strftime("%Y-%m-%d"),
    )

    await log_action(
        db,
        actor=user.email,
        action="roster.exports.download",
        module="roster",
        target=exp.id,
        details=f"rows={row_count} filename={filename} for_app={exp.for_app}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Row-Count": str(row_count - 1),
        },
    )
