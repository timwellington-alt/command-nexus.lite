"""Clever Custom Sections — status page + manual sync trigger.

Reachable via Settings → Roster → Clever Custom Sections. Two endpoints:

  GET  /settings/clever-custom-sections           — dashboard
  POST /api/roster/custom-sections/sync            — trigger a run

The sync trigger runs the worker inline (not via ARQ enqueue) so
the operator gets an immediate result. Nightly runs still go
through ARQ per the scheduler.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import User
from app.policies.engine import get_user_permissions, require_action
from app.policies.page_context import build_page_modules

logger = logging.getLogger(__name__)
router = APIRouter(tags=["roster", "custom-sections"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings/clever-custom-sections", response_class=HTMLResponse)
async def custom_sections_page(
    request: Request,
    user: User = Depends(require_action("roster.custom_sections.manage")),
    db: AsyncSession = Depends(get_db),
):
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)

    # Pull last-N audit rows for the dashboard's history block
    history = (await db.execute(text("""
        SELECT actor, action, target, details, created_at
        FROM audit_logs
        WHERE action LIKE 'roster.custom_sections.%'
        ORDER BY created_at DESC
        LIMIT 15
    """))).mappings().all()

    def _parse_details(d):
        if not d:
            return {}
        if isinstance(d, dict):
            return d
        try:
            return json.loads(d)
        except (TypeError, ValueError):
            return {}

    events = [
        {
            "at": r["created_at"].isoformat(),
            "actor": r["actor"],
            "action": r["action"],
            "target": r["target"],
            "details": _parse_details(r["details"]),
        }
        for r in history
    ]

    from app.modules.settings.repository import get_setting_value
    sheet_id = (await get_setting_value(db, "clever_custom_sections", "sheet_id") or "").strip()
    sync_enabled = (
        await get_setting_value(db, "clever_custom_sections", "sync_enabled")
        or ""
    ).strip().lower() == "true"

    return templates.TemplateResponse("settings_clever_custom_sections.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "sheet_id": sheet_id,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else "",
        "sync_enabled": sync_enabled,
        "events": events,
    })


@router.post("/api/roster/custom-sections/sync")
async def trigger_sync(
    user: User = Depends(require_action("roster.custom_sections.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Fire the sync job inline so the operator sees the outcome now.

    Runs the same code as the nightly ARQ job — dry-run vs live is
    still controlled by clever_custom_sections.sync_enabled.
    """
    from app.workers.clever_custom_sections_job import sync_clever_custom_sections
    result = await sync_clever_custom_sections({"actor": user.email})
    return result
