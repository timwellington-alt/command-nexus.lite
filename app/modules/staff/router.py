"""
Staff module router — directory, requests, provisioning, media.

All write operations audited. Temp passwords never persisted.
"""

import asyncio
import json
import logging
import os

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_db
from app.db.models import User
from app.policies.engine import require_action, get_user_permissions, check_permission
from app.audit.service import log_action
from app.modules.staff.schemas import (
    OnboardRequest, OffboardRequest,
    UpdatePaxtonAccessRequest, GoogleGroupRequest, DisableRequest,
    IgnoreRequest, MoveADRequest, UpdateTitleRequest, ConfirmLinkRequest,
)
from app.modules.staff import repository as repo
from app.policies.page_context import build_page_modules
from app.modules.staff.nicknames import FORMAL_TO_NICKS

logger = logging.getLogger(__name__)

# Backward-compat alias used throughout this file
_FORMAL_TO_NICKS = FORMAL_TO_NICKS

router = APIRouter(tags=["staff"])
templates = Jinja2Templates(directory="app/templates")


# ── Pages ─────────────────────────────────────────────────────────────────

@router.get("/staff", response_class=HTMLResponse)
async def staff_page(
    request: Request,
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    can_execute = check_permission(permissions, "staff.provision.execute")
    can_queue_view = check_permission(permissions, "staff.queue.view") or can_execute
    can_queue_edit = check_permission(permissions, "staff.queue.edit") or can_execute
    return templates.TemplateResponse("staff.html", {
        "request": request,
        "user": user,
        "modules": modules,
        "can_execute": can_execute,
        "can_queue_view": can_queue_view,
        "can_queue_edit": can_queue_edit,
    })


@router.get("/staff/intake")
async def intake_page():
    """Redirect to staff page — intake form is now in the New Hire tab."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/staff", status_code=302)


@router.get("/staff/id-cards", response_class=HTMLResponse)
async def id_cards_page(
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Queue of staff who need a Paxton ID card printed."""
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    return templates.TemplateResponse("staff_id_cards.html", {
        "request": request,
        "user": user,
        "modules": modules,
    })


@router.get("/api/staff/id-cards")
async def list_id_card_queue(
    status: str = Query("ready", regex="^(ready|pending_photo|printed|all)$"),
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """List staff_queue rows in the ID-card pipeline. `status=ready` is
    the default = "photos processed OK, waiting to be printed."""
    from sqlalchemy import text
    if status == "all":
        where = "id_card_status IS NOT NULL AND id_card_status <> 'skipped'"
    else:
        where = "id_card_status = :st"
    rows = (await db.execute(text(f"""
        SELECT id, first_name, last_name, expected_email, email, building,
               position, title, photo_path,
               id_card_status, id_card_updated_at, id_card_printed_by
        FROM staff_queue
        WHERE {where}
        ORDER BY id_card_updated_at DESC NULLS LAST, id DESC
        LIMIT 200
    """).bindparams(**({"st": status} if status != "all" else {})))).mappings().all()
    return {"entries": [dict(r) for r in rows]}


@router.post("/api/staff/id-cards/{item_id}/mark-printed")
async def mark_id_card_printed(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text
    row = (await db.execute(text(
        "SELECT id, id_card_status FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if row[1] not in ("ready", "printed"):
        raise HTTPException(status_code=409, detail=f"Cannot mark printed from '{row[1]}' status")
    await db.execute(text("""
        UPDATE staff_queue
        SET id_card_status = 'printed',
            id_card_updated_at = NOW(),
            id_card_printed_by = :by
        WHERE id = :id
    """).bindparams(id=item_id, by=user.email))
    await log_action(
        db, actor=user.email, action="staff.id_card.printed",
        module="staff", target=f"queue#{item_id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/staff/id-cards/{item_id}/skip")
async def skip_id_card(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Operator dismisses this row — no card needed (contractor, temp,
    already has one, etc). Clears from the queue view."""
    from sqlalchemy import text
    await db.execute(text("""
        UPDATE staff_queue
        SET id_card_status = 'skipped',
            id_card_updated_at = NOW(),
            id_card_printed_by = :by
        WHERE id = :id
    """).bindparams(id=item_id, by=user.email))
    await log_action(
        db, actor=user.email, action="staff.id_card.skipped",
        module="staff", target=f"queue#{item_id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


# ── API: Requests ─────────────────────────────────────────────────────────

@router.post("/api/staff/requests/onboard")
async def submit_onboard(
    body: OnboardRequest,
    request: Request,
    user: User = Depends(require_action("staff.request.submit")),
    db: AsyncSession = Depends(get_db),
):
    """Submit an onboarding request. Does NOT execute provisioning."""
    try:
        req = await repo.create_request(
            db,
            request_type="onboard",
            first_name=body.first_name,
            last_name=body.last_name,
            building=body.building,
            role_type=body.role_type,
            title=body.title,
            start_date=body.start_date,
            state_id=body.state_id,
            phone=body.phone,
            room_number=body.room_number,
            needs_sis=body.needs_sis,
            notes=body.notes,
            submitted_by=user.email,
        )
        await log_action(db,
            actor=user.email, action="staff.request.submit", module="staff",
            target=f"{body.first_name} {body.last_name}",
            details=f"type=onboard, building={body.building}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok", "id": req.id}
    except Exception:
        await db.rollback()
        raise


@router.post("/api/staff/requests/offboard")
async def submit_offboard(
    body: OffboardRequest,
    request: Request,
    user: User = Depends(require_action("staff.request.submit")),
    db: AsyncSession = Depends(get_db),
):
    """Submit an offboarding request."""
    try:
        req = await repo.create_request(
            db,
            request_type="offboard",
            first_name=body.first_name,
            last_name=body.last_name,
            building=body.building,
            role_type="offboard",
            existing_email=body.existing_email,
            existing_ad_username=body.existing_ad_username,
            notes=body.notes,
            submitted_by=user.email,
        )
        await log_action(db,
            actor=user.email, action="staff.request.submit", module="staff",
            target=f"{body.first_name} {body.last_name}",
            details="type=offboard",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok", "id": req.id}
    except Exception:
        await db.rollback()
        raise


@router.get("/api/staff/requests")
async def list_requests(
    request_type: str | None = None,
    status: str | None = None,
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    requests = await repo.list_requests(db, request_type=request_type, status=status)
    return [
        {
            "id": r.id,
            "request_type": r.request_type,
            "first_name": r.first_name,
            "last_name": r.last_name,
            "building": r.building,
            "role_type": r.role_type,
            "status": r.status,
            "submitted_by": r.submitted_by,
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
        }
        for r in requests
    ]


@router.get("/api/staff/requests/{request_id}")
async def get_request_detail(
    request_id: int,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """
    Full workflow detail for an onboard/offboard request — includes
    workflow_runs with step-level errors that can leak internal API
    paths / stack traces. Restricted to staff.provision.execute
    (the perm needed to actually run the workflow) rather than the
    broader staff.view — a plain directory viewer doesn't need
    workflow internals.
    """
    req = await repo.get_request(db, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    runs = await repo.get_workflow_runs(db, request_id)
    run_data = []
    for run in runs:
        steps = await repo.get_workflow_steps(db, run.id)
        run_data.append({
            "id": run.id,
            "status": run.status,
            "started_by": run.started_by,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "error_summary": run.error_summary,
            "steps": [
                {
                    "step_name": s.step_name,
                    "status": s.status,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                    "error_message": s.error_message,
                }
                for s in steps
            ],
        })

    return {
        "id": req.id,
        "request_type": req.request_type,
        "first_name": req.first_name,
        "last_name": req.last_name,
        "building": req.building,
        "role_type": req.role_type,
        "title": req.title,
        "status": req.status,
        "submitted_by": req.submitted_by,
        "submitted_at": req.submitted_at.isoformat() if req.submitted_at else None,
        "workflow_runs": run_data,
    }


# ── API: Workflow Execution ───────────────────────────────────────────────

@router.post("/api/staff/requests/{request_id}/execute")
async def execute_request(
    request_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Execute provisioning for a staff request. Runs workflow steps."""
    from app.modules.staff.service import execute_onboard, execute_offboard

    req = await repo.get_request(db, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status not in ("pending", "partial"):
        raise HTTPException(status_code=400, detail=f"Cannot execute request in '{req.status}' status")

    try:
        if req.request_type == "onboard":
            result = await execute_onboard(db, request_id, user.email)
        elif req.request_type == "offboard":
            result = await execute_offboard(db, request_id, user.email)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown request type: {req.request_type}")

        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise


@router.post("/api/staff/requests/{request_id}/cancel")
async def cancel_request(
    request_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    req = await repo.get_request(db, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending requests can be cancelled")

    try:
        req.status = "cancelled"
        await log_action(db,
            actor=user.email, action="staff.request.cancel", module="staff",
            target=f"{req.first_name} {req.last_name}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except Exception:
        await db.rollback()
        raise


# ── T8.10: Authenticated Media ────────────────────────────────────────────

import os
import re

MEDIA_DIR = os.environ.get("MEDIA_DIR", "data/photos")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5MB


@router.post("/api/staff/requests/{request_id}/photo")
async def upload_photo(
    request_id: int,
    photo: UploadFile = File(...),
    user: User = Depends(require_action("staff.request.submit")),
    db: AsyncSession = Depends(get_db),
):
    """Upload a staff photo. Validates type, size, and image content."""
    req = await repo.get_request(db, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    # Validate extension
    ext = os.path.splitext(photo.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type not allowed: {ext}")

    # Read and validate size
    content = await photo.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    # Validate image content (check magic bytes)
    if not _is_valid_image(content):
        raise HTTPException(status_code=400, detail="Invalid image content")

    os.makedirs(MEDIA_DIR, exist_ok=True)
    filename = f"staff_{request_id}{ext}"
    filepath = os.path.join(MEDIA_DIR, filename)

    with open(filepath, "wb") as f:
        f.write(content)

    try:
        req.photo_path = f"/api/staff/media/{filename}"
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {"status": "ok", "photo_path": req.photo_path}


@router.get("/api/staff/media/{filename}")
async def serve_media(
    filename: str,
    user: User = Depends(require_action("staff.view")),
):
    """Serve staff photos through authenticated route. Never via static mount."""
    if not re.match(r'^[\w\-\.]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    filepath = os.path.join(MEDIA_DIR, filename)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Photo not found")

    return FileResponse(filepath, media_type="image/jpeg")


def _is_valid_image(content: bytes) -> bool:
    """Check magic bytes to verify image content."""
    if content[:3] == b'\xff\xd8\xff':  # JPEG
        return True
    if content[:8] == b'\x89PNG\r\n\x1a\n':  # PNG
        return True
    return False


def _resolve_active_photo(username: str) -> str | None:
    """Return the filename of the user's active photo, or None.

    Prefers the explicit `_active.jpg` marker. Falls back to the newest
    `profile_{username}_2*.jpg` upload if the marker is missing — this
    catches users whose marker was never created (mid-dev state). The
    fallback is read-only; it does not create the marker on the fly to
    keep media reads side-effect-free.
    """
    if not username:
        return None
    import glob
    safe = username.replace("/", "").replace("\\", "")
    active_name = f"profile_{safe}_active.jpg"
    if os.path.isfile(os.path.join(MEDIA_DIR, active_name)):
        return active_name
    matches = sorted(
        glob.glob(os.path.join(MEDIA_DIR, f"profile_{safe}_2*.jpg")),
        reverse=True,
    )
    if matches:
        return os.path.basename(matches[0])
    return None


def _photo_cache_buster(filename: str) -> str:
    """Append ?v={mtime_ms} so the browser refetches when the file changes.
    Millisecond precision avoids same-second collisions when a new upload
    is fetched immediately after writing."""
    try:
        m = int(os.path.getmtime(os.path.join(MEDIA_DIR, filename)) * 1000)
        return f"?v={m}"
    except OSError:
        return ""


# ── API: Staff Directory (from Google Workspace) ─────────────────────────

# ── Staff alerts (ticket-call recipient toggle on the staff profile) ────
# Lets an admin toggle on alerts for a maintenance/custodial staff member
# who won't OAuth-log-in. Lazily creates a User row + a VoiceRecipient
# linked to it. Sensible defaults: severity=high (urgent tickets only),
# quiet 16:00–07:00, quiet_weekends=true, no PIN, push disabled. The
# user's UCM extension is snapshotted from phone_extension_cache so the
# urgent-alert flow's parallel desk-and-cell dial works automatically.

@router.get("/api/staff/{staff_email}/alerts")
async def get_staff_alerts(
    staff_email: str,
    user: User = Depends(require_action("voice.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Return the alert-recipient state for a staff member by email.
    {enabled, cell_e164, extension_from_ucm, severity_threshold,
     quiet_hours_start, quiet_hours_end, quiet_weekends}.
    enabled=false when no User row or no linked VoiceRecipient exists."""
    from app.modules.alerts import models as _alerts_models  # noqa: F401 — registry
    from app.modules.voice.models import VoiceRecipient
    from sqlalchemy import text as _text
    email = (staff_email or "").strip().lower()
    # UCM extension — staff_directory.extension is the canonical UCM-
    # synced field (same source the rest of the profile uses). Fall
    # back to phone_extension_cache only if staff_directory didn't
    # have it for some reason.
    ext_row = (await db.execute(_text(
        "SELECT extension FROM staff_reconciliation "
        "WHERE LOWER(email) = :e LIMIT 1"
    ), {"e": email})).first()
    ucm_ext = ext_row[0] if ext_row else None
    if not ucm_ext:
        fb = (await db.execute(_text(
            "SELECT extension FROM phone_extension_cache "
            "WHERE LOWER(email) = :e LIMIT 1"
        ), {"e": email})).first()
        ucm_ext = fb[0] if fb else None

    u = (await db.execute(select(User).where(User.email.ilike(email)))).scalar_one_or_none()
    if not u or not u.voice_recipient_id:
        return {
            "enabled": False, "cell_e164": None,
            "extension_from_ucm": ucm_ext,
            "severity_threshold": None,
            "quiet_hours_start": None, "quiet_hours_end": None,
            "quiet_weekends": None,
        }
    r = (await db.execute(
        select(VoiceRecipient).where(VoiceRecipient.id == u.voice_recipient_id)
    )).scalar_one_or_none()
    if not r:
        return {
            "enabled": False, "cell_e164": None,
            "extension_from_ucm": ucm_ext,
            "severity_threshold": None,
            "quiet_hours_start": None, "quiet_hours_end": None,
            "quiet_weekends": None,
        }
    return {
        "enabled": bool(r.active),
        "cell_e164": r.number_e164,
        "extension_from_ucm": ucm_ext,
        "severity_threshold": r.severity_threshold,
        "quiet_hours_start": r.quiet_hours_start.strftime("%H:%M") if r.quiet_hours_start else None,
        "quiet_hours_end": r.quiet_hours_end.strftime("%H:%M") if r.quiet_hours_end else None,
        "quiet_weekends": r.quiet_weekends,
    }


@router.patch("/api/staff/{staff_email}/alerts")
async def set_staff_alerts(
    staff_email: str,
    payload: dict,
    request: Request,
    user: User = Depends(require_action("voice.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Toggle alerts on/off for a staff member and update their cell.
    Body: {enabled: bool, cell_e164: str|null}. Side-effects:
      - Lazy-create User row if missing
      - Lazy-create + link VoiceRecipient with the documented defaults
      - Snapshot UCM extension into recipient.extension at save time
      - active=true on enable, active=false on disable (preserves
        contact info so re-enabling doesn't lose prefs)"""
    from datetime import time as _time
    from app.modules.alerts import models as _alerts_models  # noqa: F401
    from app.modules.voice.models import VoiceRecipient
    from sqlalchemy import text as _text
    from app.modules.voice.service import push_ivr_whitelist

    email = (staff_email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="valid staff_email required")
    enabled = bool(payload.get("enabled"))
    cell = (payload.get("cell_e164") or "").strip() or None
    if cell and not (cell.startswith("+") and cell[1:].isdigit()):
        raise HTTPException(status_code=400, detail="cell_e164 must be +<digits> (E.164)")

    # Verify the email is in staff_directory so we don't create
    # phantoms — this endpoint is "add a real staff member as a call
    # recipient," not "create arbitrary user."
    sd = (await db.execute(_text(
        "SELECT full_name FROM staff_directory WHERE LOWER(email)=:e LIMIT 1"
    ), {"e": email})).first()
    if not sd:
        raise HTTPException(status_code=404, detail=f"{email} not in staff directory")
    full_name = sd[0] or email

    # Snapshot the UCM extension at save time. Prefer
    # staff_directory.extension (canonical, UCM-synced) over
    # phone_extension_cache. Stays static until the next save.
    ext_row = (await db.execute(_text(
        "SELECT extension FROM staff_reconciliation "
        "WHERE LOWER(email) = :e LIMIT 1"
    ), {"e": email})).first()
    ucm_ext = ext_row[0] if ext_row else None
    if not ucm_ext:
        fb = (await db.execute(_text(
            "SELECT extension FROM phone_extension_cache "
            "WHERE LOWER(email) = :e LIMIT 1"
        ), {"e": email})).first()
        ucm_ext = fb[0] if fb else None

    # Lazy-create the User row.
    u = (await db.execute(select(User).where(User.email.ilike(email)))).scalar_one_or_none()
    if not u:
        u = User(email=email, name=full_name)
        db.add(u)
        await db.flush()

    # Lazy-create the linked VoiceRecipient with the staff-defaults.
    r = None
    if u.voice_recipient_id:
        r = (await db.execute(
            select(VoiceRecipient).where(VoiceRecipient.id == u.voice_recipient_id)
        )).scalar_one_or_none()
    if not r:
        r = VoiceRecipient(
            label=full_name,
            number_e164=cell,
            extension=ucm_ext,
            severity_threshold="high",  # urgent tickets fire severity=high
            quiet_hours_start=_time(16, 0),
            quiet_hours_end=_time(7, 0),
            quiet_weekends=True,
            quiet_hours_override_severity=None,
            active=enabled,
            voice_enabled=True,
            push_enabled=False,  # no PWA for these users
            priority_order=200,
        )
        db.add(r)
        await db.flush()
        u.voice_recipient_id = r.id
    else:
        r.label = full_name
        r.number_e164 = cell
        r.extension = ucm_ext
        r.active = enabled

    await log_action(
        db, actor=user.email, action="staff.alerts.set",
        module="staff", target=email,
        details=f"enabled={enabled} cell={cell or '-'} ucm_ext={ucm_ext or '-'}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    # Refresh IVR whitelist so the new recipient (or removed one) takes
    # effect on the next inbound call.
    try:
        await push_ivr_whitelist(db)
    except Exception as e:
        logger.warning(f"IVR whitelist push failed after staff alert update: {e}")
    return {
        "ok": True, "enabled": enabled,
        "cell_e164": cell, "extension_from_ucm": ucm_ext,
    }


@router.get("/api/staff/directory/2fa-not-enrolled.csv")
async def export_2fa_not_enrolled_csv(
    building: str | None = None,
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """CSV of confirmed staff who are NOT enrolled in 2FA.

    Matches the same "no_2fa" filter the directory UI shows — confirmed
    match_state (hr_match / roster_match / override), skipping ignored
    rows, unmatched accounts, and non-person entries. Optional
    `building` query narrows to one school so ops can batch notifications
    by building.

    Includes on_leave staff with a flag in the row — the notifier can
    filter them out before sending (probably don't page someone on
    LOA about a new IT policy), but they're kept in the export so
    they're not silently dropped.
    """
    import csv
    import io
    from sqlalchemy import select, or_
    from fastapi.responses import PlainTextResponse
    from app.modules.staff.models import StaffReconciliation

    q = (select(StaffReconciliation)
         .where(StaffReconciliation.ignored == False)  # noqa: E712
         .where(StaffReconciliation.match_state.in_(["hr_match", "roster_match", "override"]))
         .where(or_(
             StaffReconciliation.is_enrolled_in_2sv == False,  # noqa: E712
             StaffReconciliation.is_enrolled_in_2sv.is_(None),
         ))
         .order_by(StaffReconciliation.building, StaffReconciliation.last_name,
                   StaffReconciliation.first_name))
    if building:
        q = q.where(StaffReconciliation.building == building)

    rows = (await db.execute(q)).scalars().all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "email", "first_name", "last_name", "full_name",
        "building", "title", "role_type", "match_state",
        "is_enrolled_in_2sv", "is_enforced_in_2sv",
        "on_leave", "hr_notes", "last_login",
    ])
    for s in rows:
        w.writerow([
            s.email or "",
            s.first_name or "",
            s.last_name or "",
            s.full_name or f"{s.first_name or ''} {s.last_name or ''}".strip(),
            s.building or "",
            s.title or "",
            s.role_type or "",
            s.match_state or "",
            "" if s.is_enrolled_in_2sv is None else str(s.is_enrolled_in_2sv).lower(),
            "" if s.is_enforced_in_2sv is None else str(s.is_enforced_in_2sv).lower(),
            "true" if s.on_leave else "false",
            (s.hr_notes or "").replace("\n", " ").strip(),
            s.last_login or "",
        ])

    from datetime import datetime as _dt, timezone as _tz
    stamp = _dt.now(_tz.utc).strftime("%Y%m%d")
    bldg_suffix = f"-{building}" if building else ""
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="2fa-not-enrolled{bldg_suffix}-{stamp}.csv"',
        },
    )


@router.get("/api/staff/directory")
async def staff_directory(
    building: str | None = None,
    status: str | None = None,
    search: str | None = None,
    role: str | None = None,
    include_ignored: bool = False,  # Deprecated — kept as a no-op for backward compat
    match: str | None = None,
    sort: str = Query("last_name"),
    sort_dir: str = Query("asc"),
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Staff directory from pre-computed reconciliation table.

    `match` filters by confirmation state OR cross-system completeness:
        confirmed (default) — hr_match OR roster_match OR override
        unmatched           — match_state = 'unmatched'
        non_person          — regex-flagged service/shared accounts
        hr_match            — only HR-confirmed
        roster_match        — only roster-confirmed
        override            — only manual override
        missing_google      — has AD/Paxton record but Google account gap
        missing_ad          — has Google but no AD account
        missing_paxton      — has Google but no Paxton cardholder
        missing_phone       — has Google but no extension assigned
        stale               — confirmed staff with no login in 90+ days
        all                 — no filter

    The missing_* filters replace the old Reconciliation tab — each one
    shows a subset of confirmed staff where a specific downstream
    system is out of sync, so operators can drill into a single gap at
    a time instead of scanning a wide comparison grid.
    """
    from datetime import timedelta
    from sqlalchemy import select, func, or_, asc, desc
    from app.modules.staff.models import StaffReconciliation

    q = select(StaffReconciliation)
    if building:
        q = q.where(StaffReconciliation.building == building)
    if status:
        q = q.where(StaffReconciliation.google_status == status)
    if search:
        term = f"%{search}%"
        q = q.where(or_(
            StaffReconciliation.first_name.ilike(term),
            StaffReconciliation.last_name.ilike(term),
            StaffReconciliation.email.ilike(term),
            StaffReconciliation.title.ilike(term),
        ))
    if role:
        # Historical role_type values in this district drift from the
        # canonical set the UI dropdown offers — HR uses "cert",
        # "class", "adm", "adm - class", "xmpt" as classification
        # codes, some rows have the full "teacher"/"classified"/etc.
        # Alias table matches both directions so the filter works
        # regardless of which format a row ended up with.
        _ROLE_ALIASES = {
            "teacher":          ("teacher", "cert"),
            "classified":       ("classified", "class"),
            "admin":            ("admin", "adm", "adm - class", "xmpt", "exempt"),
            "paraprofessional": ("paraprofessional", "aide"),
            "tech":             ("tech",),
            "sub":              ("sub",),
        }
        aliases = _ROLE_ALIASES.get(role.strip().lower(), (role.strip().lower(),))
        q = q.where(func.lower(StaffReconciliation.role_type).in_(aliases))
    if not include_ignored:
        q = q.where(StaffReconciliation.ignored == False)  # noqa: E712

    # Default to "confirmed" so the directory page never shows unmatched
    # or non_person accounts unless the user explicitly switches filters.
    match_filter = (match or "confirmed").lower()
    confirmed_states = ["hr_match", "roster_match", "override"]

    if match_filter == "confirmed":
        q = q.where(StaffReconciliation.match_state.in_(confirmed_states))
    elif match_filter in ("unmatched", "non_person", "hr_match", "roster_match", "override"):
        q = q.where(StaffReconciliation.match_state == match_filter)
    elif match_filter == "missing_google":
        q = q.where(StaffReconciliation.match_state.in_(confirmed_states))
        q = q.where(StaffReconciliation.google_ok == False)  # noqa: E712
    elif match_filter == "missing_ad":
        q = q.where(StaffReconciliation.match_state.in_(confirmed_states))
        q = q.where(or_(
            StaffReconciliation.ad_ok == False,  # noqa: E712
            StaffReconciliation.ad_username.is_(None),
        ))
    elif match_filter == "missing_paxton":
        q = q.where(StaffReconciliation.match_state.in_(confirmed_states))
        q = q.where(or_(
            StaffReconciliation.paxton_ok == False,  # noqa: E712
            StaffReconciliation.paxton_id.is_(None),
        ))
    elif match_filter == "missing_phone":
        q = q.where(StaffReconciliation.match_state.in_(confirmed_states))
        q = q.where(or_(
            StaffReconciliation.extension.is_(None),
            StaffReconciliation.extension == "",
        ))
    elif match_filter == "on_leave":
        q = q.where(StaffReconciliation.on_leave == True)  # noqa: E712
    elif match_filter == "email_mismatch":
        q = q.where(StaffReconciliation.email_mismatch == True)  # noqa: E712
    elif match_filter == "no_2fa":
        # Confirmed staff who are NOT enrolled in 2SV. Includes both
        # `False` (definitely not enrolled) and `NULL` (unknown —
        # hasn't been re-synced since the 2FA column was added).
        # Both need attention; distinguishing them is a next-pass
        # concern once the sync has run a couple times.
        q = q.where(StaffReconciliation.match_state.in_(confirmed_states))
        q = q.where(or_(
            StaffReconciliation.is_enrolled_in_2sv == False,  # noqa: E712
            StaffReconciliation.is_enrolled_in_2sv.is_(None),
        ))
    elif match_filter == "has_2fa":
        q = q.where(StaffReconciliation.match_state.in_(confirmed_states))
        q = q.where(StaffReconciliation.is_enrolled_in_2sv == True)  # noqa: E712
    elif match_filter == "stale":
        # Stale = confirmed staff whose last_login is older than 90 days.
        # last_login is stored as an ISO-8601 string so we compare
        # lexicographically — valid because the format is sortable.
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=90)
        ).isoformat()
        q = q.where(StaffReconciliation.match_state.in_(confirmed_states))
        q = q.where(or_(
            StaffReconciliation.last_login.is_(None),
            StaffReconciliation.last_login == "",
            StaffReconciliation.last_login < cutoff_iso,
        ))
    # match_filter == "all" → no filter

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    # Count ignored separately for the response
    ignored_count = (await db.execute(
        select(func.count()).where(StaffReconciliation.ignored == True)  # noqa: E712
    )).scalar_one()

    SORT_COLS = {
        "last_name": StaffReconciliation.last_name,
        "first_name": StaffReconciliation.first_name,
        "display_name": StaffReconciliation.last_name,
        "email": StaffReconciliation.email,
        "title": StaffReconciliation.title,
        "building": StaffReconciliation.building,
        "last_login": StaffReconciliation.last_login,
        "status": StaffReconciliation.google_status,
        "extension": StaffReconciliation.extension,
        "room": StaffReconciliation.room,
    }
    sort_col = SORT_COLS.get(sort, StaffReconciliation.last_name)
    order_fn = desc if sort_dir == "desc" else asc
    q = q.order_by(order_fn(sort_col), asc(StaffReconciliation.first_name)).offset(offset).limit(limit)
    result = await db.execute(q)
    staff = result.scalars().all()

    # Overlay live UCM phone status — try cache first, warm if cold
    live_phone_status = {}
    try:
        from app.integrations.grandstream.adapter import _state as _ucm_state, CACHE_TTL, ucm_session
        import time as _time
        # If cache is cold, warm it (one SSH call, shared across all requests for 30s)
        if not _ucm_state.extensions_cache or (_time.time() - _ucm_state.extensions_cache_time) >= CACHE_TTL:
            try:
                from app.modules.settings.repository import get_setting_value as _gsv2
                ucm_url = await _gsv2(db, "grandstream", "url")
                if ucm_url:
                    async with ucm_session(db) as ucm:
                        await ucm.list_extensions()
            except Exception:
                pass
        # Read from cache (lock-free)
        if _ucm_state.extensions_cache and (_time.time() - _ucm_state.extensions_cache_time) < CACHE_TTL:
            for ext in _ucm_state.extensions_cache:
                live_phone_status[ext.get("extension", "")] = {
                    "status": ext.get("status", ""),
                    "registered": ext.get("registered", False),
                    "ip": ext.get("ip", ""),
                    "model": ext.get("model", ""),
                }
    except Exception:
        pass

    staff_list = []
    for s in staff:
        entry = {
            "id": s.id,
            "email": s.email,
            "google_email": s.email,
            "username": s.username,
            "first_name": s.first_name,
            "last_name": s.last_name,
            "full_name": s.full_name,
            "display_name": s.display_name,
            "title": s.title,
            "department": s.department,
            "building": s.building,
            "org_unit": s.org_unit,
            "phone": s.phone,
            "status": s.google_status,
            "enabled": s.google_status == "active",
            "is_admin": s.is_admin,
            "last_login": s.last_login,
            "hr_active": s.hr_active,
            "hr_notes": s.hr_notes,
            "on_leave": s.on_leave,
            "email_mismatch": s.email_mismatch,
            # 2FA / 2SV enrollment + enforcement. Tri-state (True/False/None):
            # None = we haven't fetched yet (fresh install / user missing
            # from last sync). UI renders ✓ / ✗ / — accordingly.
            "is_enrolled_in_2sv": s.is_enrolled_in_2sv,
            "is_enforced_in_2sv": s.is_enforced_in_2sv,
            "google_ok": s.google_ok,
            "ad_ok": s.ad_ok or False,
            "sis_ok": s.sis_ok,
            "paxton_ok": s.paxton_ok,
            "paxton_id": s.paxton_id,
            "role_type": s.role_type,
            "hr_position": s.hr_position,
            "hr_school": s.hr_school,
            "hr_classification": s.hr_classification,
            "ignored": s.ignored,
            "match_state": s.match_state,
        }
        # Photo — resolved from the staff_photos legacy marker (dual-
        # written by photo_service on upload/set-active) so this hot
        # loop stays sync. Cache-buster (?v=mtime) so the browser
        # refetches when the file changes.
        local_photo = _resolve_active_photo(s.username)
        if local_photo:
            entry["photo"] = f"/api/staff/media/{local_photo}{_photo_cache_buster(local_photo)}"
        # Room
        if s.room:
            entry["room"] = s.room
            entry["room_assignment"] = s.room_assignment
            entry["room_floor"] = s.room_floor
            entry["room_building"] = s.room_building
            if s.is_esc:
                entry["is_esc"] = True
        # Extension / phone — live from UCM cache if available, static fallback
        if s.extension:
            entry["extension"] = s.extension
            live = live_phone_status.get(s.extension)
            if live:
                entry["phone_registered"] = live["registered"]
                entry["phone_status"] = live["status"]
                entry["phone_ip"] = live["ip"]
                entry["phone_model"] = live["model"]
            else:
                entry["phone_registered"] = s.phone_registered
                entry["phone_status"] = "idle" if s.phone_registered else "unavailable"
                entry["phone_ip"] = s.phone_ip or ""
                entry["phone_model"] = s.phone_model or ""
        # Last door
        if s.last_door_time:
            entry["last_door"] = {
                "time": s.last_door_time,
                "door": s.last_door_name,
                "building": s.last_door_building,
            }
        # Name sync issues
        if s.name_sync_issues:
            issues = json.loads(s.name_sync_issues)
            google_name = f"{(s.first_name or '').strip()} {(s.last_name or '').strip()}".strip()
            entry["name_sync"] = {"google_name": google_name, "issues": issues}
            ucm_issue = next((i for i in issues if i["system"] == "ucm"), None)
            if ucm_issue:
                entry["ucm_name_mismatch"] = {
                    "ucm_name": ucm_issue["current"],
                    "expected_name": google_name,
                    "extension": ucm_issue.get("extension"),
                }
        # Room-ext mismatch
        if s.room_ext_mismatch:
            entry["room_ext_mismatch"] = json.loads(s.room_ext_mismatch)

        staff_list.append(entry)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "hr_loaded": any(s.hr_active is not None for s in staff),
        "ignored": ignored_count,
        "staff": staff_list,
    }


# ── API: HR Sheet ────────────────────────────────────────────────────────

@router.get("/api/staff/hr-sheet")
async def get_hr_sheet(
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Read active staff from the HR data source.

    Tries SMB/Excel first (if configured), falls back to Google Sheets.
    Both return the same dict shape: {first_name, last_name, email, school, classification, position}.
    """
    from app.modules.settings.repository import get_setting_value

    # Try SMB/Excel first
    smb_server = await get_setting_value(db, "hr_smb", "server")
    if smb_server:
        try:
            from app.integrations.smb.adapter import SmbExcelAdapter
            adapter = SmbExcelAdapter(db)
            staff = await adapter.read_hr_staff()
            return {"total": len(staff), "staff": staff, "source": "smb"}
        except Exception as e:
            logger.warning(f"SMB HR read failed, trying Google Sheets: {e}")

    # Fall back to Google Sheets
    sheet_id = await get_setting_value(db, "google", "hr_sheet_id")
    if not sheet_id:
        return {"total": 0, "staff": [], "error": "No HR source configured. Set up SMB Share or Google Sheet ID in Settings."}

    try:
        from app.integrations.google.sheets_adapter import GoogleSheetsAdapter
        adapter = GoogleSheetsAdapter(db)
        staff = await adapter.read_hr_staff(sheet_id)
        return {"total": len(staff), "staff": staff, "source": "google_sheets"}
    except Exception as e:
        return {"total": 0, "staff": [], "error": str(e)[:200]}


@router.get("/api/staff/hr-smb-tabs")
async def get_hr_smb_tabs(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """List available tab names from the HR Excel file on SMB."""
    try:
        from app.integrations.smb.adapter import SmbExcelAdapter
        adapter = SmbExcelAdapter(db)
        tabs = await adapter.list_tabs()
        return {"tabs": tabs}
    except Exception as e:
        return {"tabs": [], "error": str(e)[:150]}


@router.get("/api/staff/hr-smb-tab-configs")
async def get_hr_tab_configs(
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Get configured tab→column mappings for the HR Excel import."""
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "hr_smb", "tab_configs") or "[]"
    try:
        configs = json.loads(raw)
    except Exception:
        configs = []
    return {"configs": configs}


@router.post("/api/staff/hr-smb-tab-configs")
async def save_hr_tab_configs(
    request: Request,
    user: User = Depends(require_action("settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Save tab→column mappings for the HR Excel import."""
    from app.modules.settings.repository import upsert_integration_setting
    body = await request.json()
    configs = body.get("configs", [])
    await upsert_integration_setting(
        db, integration="hr_smb", key="tab_configs",
        value=json.dumps(configs), is_secret_ref=False,
        updated_by=user.email,
    )
    await db.commit()
    return {"status": "ok", "tabs": len(configs)}


# ── API: Integration Metadata (for profile builder dropdowns) ────────────

@router.get("/api/staff/paxton-metadata")
async def paxton_metadata(
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Return Paxton access levels, door groups, and departments for profile builder dropdowns."""
    from app.integrations.paxton.adapter import PaxtonAdapter
    paxton = PaxtonAdapter(db)
    try:
        levels = await paxton.get_access_levels()
        doors = await paxton.list_doors()
        departments = await paxton.list_departments()
        return {
            "access_levels": [
                {"id": l.get("id"), "name": l.get("name", f"ID {l.get('id')}")}
                for l in levels if isinstance(l, dict)
            ],
            "doors": [
                {"id": d["paxton_door_id"], "name": d["name"]}
                for d in doors
            ],
            "departments": departments,
        }
    except Exception as e:
        return {"access_levels": [], "doors": [], "departments": [], "error": str(e)[:100]}


@router.get("/api/staff/google-metadata")
async def google_metadata(
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Return Google groups and OUs for profile builder dropdowns."""
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    google = GoogleWorkspaceAdapter(db)
    result = {"groups": [], "ous": []}
    try:
        groups = await google.list_groups()
        result["groups"] = [{"email": g["email"], "name": g["name"]} for g in groups]
    except Exception as e:
        result["groups_error"] = str(e)[:100]
    try:
        ous = await google.list_ous()
        result["ous"] = [{"path": o["path"], "name": o["name"]} for o in ous]
    except Exception as e:
        result["ous_error"] = str(e)[:100]
    return result


@router.get("/api/staff/ad-metadata")
async def ad_metadata(
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Return AD groups and OUs for profile builder dropdowns."""
    from app.integrations.ad.adapter import ActiveDirectoryAdapter
    ad = ActiveDirectoryAdapter(db)
    result = {"groups": [], "ous": []}
    try:
        groups = await ad.list_groups()
        result["groups"] = [{"name": g["name"], "dn": g["dn"]} for g in groups]
    except Exception as e:
        result["groups_error"] = str(e)[:100]
    try:
        ous = await ad.list_ous()
        result["ous"] = [{"name": o["name"], "dn": o["dn"]} for o in ous]
    except Exception as e:
        result["ous_error"] = str(e)[:100]
    return result


# ── API: Provisioning Profiles ───────────────────────────────────────────

from pydantic import BaseModel as PydanticBase, field_validator

# Any character that would make an email-address local part invalid,
# or that would silently succeed-then-404 when we append @domain and
# call the Google Directory API. Whitespace anywhere in the group name
# was the original rogue-space bug (2026-07-22): an entry "PHS Teachers"
# rendered as "phs teachers@yourdistrict.org" which is not a legal
# address, so Google returned 404 on every PHS teacher provision.
_INVALID_GOOGLE_GROUP_CHARS = set(" \t\n\r()<>[]:,;\"\\")

# AD groups are different: Windows CN routinely contains spaces
# ("Domain Admins", "OpenRoom Users", "the district-PHS Teachers"). Only the
# chars that break LDAP/CSV parsing are forbidden. Leading/trailing
# spaces still get trimmed.
_INVALID_AD_GROUP_CHARS = set("\t\n\r<>:;\"\\")


def _clean_group_list(v: object, *, allow_spaces: bool = False) -> list[str]:
    """Trim + de-empty + reject invalid chars.

    Applied to google_groups (allow_spaces=False) and ad_groups
    (allow_spaces=True) on the profile-save endpoint. A typo in the
    Settings UI can never make it into a live provision workflow —
    operator gets a specific fix hint with the offending character
    named instead of a mystery Google 404 four days later.
    """
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError("must be a list of group name strings")
    bad_set = _INVALID_AD_GROUP_CHARS if allow_spaces else _INVALID_GOOGLE_GROUP_CHARS
    hint = (
        "AD group entries may contain spaces (e.g. 'OpenRoom Users'). "
        "Only control characters and delimiter punctuation are rejected."
        if allow_spaces else
        "Google-group entries must be a bare local part (e.g. "
        "'the district-PHSTeachers') or a full email — no spaces anywhere."
    )
    out: list[str] = []
    for i, raw in enumerate(v):
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        bad = bad_set & set(s)
        if bad:
            raise ValueError(
                f"group name {raw!r} (index {i}) contains invalid characters "
                f"{sorted(bad)!r}. {hint} Trailing whitespace is auto-trimmed."
            )
        out.append(s)
    return out


class ProfileUpdate(PydanticBase):
    building: str
    role_type: str
    google_ou: str | None = None
    google_groups: list[str] = []
    ad_ou: str | None = None
    ad_groups: list[str] = []
    paxton_access_level: str | None = None
    paxton_department_id: int | None = None
    paxton_department_name: str | None = None

    @field_validator("google_groups", mode="before")
    @classmethod
    def _validate_google_groups(cls, v):
        return _clean_group_list(v, allow_spaces=False)

    @field_validator("ad_groups", mode="before")
    @classmethod
    def _validate_ad_groups(cls, v):
        return _clean_group_list(v, allow_spaces=True)


@router.get("/api/staff/provisioning-profiles")
async def get_provisioning_profiles(
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """List all provisioning profiles — building+role → group mappings."""
    from app.modules.staff.provisioning_profiles import list_profiles
    return await list_profiles(db)


@router.post("/api/staff/provisioning-profiles")
async def save_provisioning_profile(
    body: ProfileUpdate,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Create or update a provisioning profile."""
    from app.modules.staff.provisioning_profiles import upsert_profile
    try:
        await upsert_profile(
            db,
            building=body.building,
            role_type=body.role_type,
            google_ou=body.google_ou,
            google_groups=body.google_groups,
            ad_ou=body.ad_ou,
            ad_groups=body.ad_groups,
            paxton_access_level=body.paxton_access_level,
            paxton_department_id=body.paxton_department_id,
            paxton_department_name=body.paxton_department_name,
            updated_by=user.email,
        )
        await log_action(
            db, actor=user.email, action="staff.provisioning_profile.save",
            module="staff", target=f"{body.building}/{body.role_type}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except Exception:
        await db.rollback()
        raise


@router.delete("/api/staff/provisioning-profiles/{profile_id}")
async def delete_provisioning_profile(
    profile_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a provisioning profile."""
    from app.modules.staff.provisioning_profiles import delete_profile
    try:
        deleted = await delete_profile(db, profile_id)
        if not deleted:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Profile not found")
        await log_action(
            db, actor=user.email, action="staff.provisioning_profile.delete",
            module="staff", target=f"profile_{profile_id}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except Exception:
        await db.rollback()
        raise


# ── API: Staff Profile ───────────────────────────────────────────────────

@router.get("/api/staff/profile/{username}")
async def staff_profile(
    username: str,
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """Full cross-system profile for a staff member.

    Base data from staff_reconciliation (1 query), supplemented with:
    - AD groups (1 query if AD matched)
    - Paxton access levels (1 query if Paxton matched)
    - Live UCM phone status (cached poll if extension matched)
    - Teaching schedule (1 query)
    """
    from sqlalchemy import select, func
    from app.modules.staff.models import StaffReconciliation, ADUserCache, PaxtonUserCache, StaffLink

    # 1. Base data from reconciliation table
    recon_result = await db.execute(
        select(StaffReconciliation).where(
            func.lower(StaffReconciliation.username) == username.lower()
        )
    )
    s = recon_result.scalar_one_or_none()

    if not s:
        raise HTTPException(status_code=404, detail=f"User not found: {username}")

    email = s.email.lower()

    # Build AD dict — from cache if matched, else from reconciliation data
    ad_user = None
    if s.ad_username:
        ad_cached = await db.execute(
            select(ADUserCache).where(ADUserCache.username == s.ad_username)
        )
        ad_row = ad_cached.scalar_one_or_none()
        if ad_row:
            ad_user = {
                "username": ad_row.username, "email": ad_row.email,
                "first_name": ad_row.first_name, "last_name": ad_row.last_name,
                "display_name": ad_row.display_name, "title": ad_row.title,
                "department": ad_row.department, "ou": ad_row.ou,
                "enabled": ad_row.enabled,
                "groups": json.loads(ad_row.groups) if ad_row.groups else [],
                "last_logon": ad_row.last_logon,
            }
    if not ad_user:
        # Staff not in AD (ESC staff etc.) — build from reconciliation
        ad_user = {
            "username": s.username, "email": s.email,
            "first_name": s.first_name, "last_name": s.last_name,
            "display_name": s.display_name, "title": s.title,
            "department": s.department, "building": s.building,
            "enabled": s.google_ok,
            "source": "google",
        }

    profile = {"ad": ad_user, "google": None, "paxton": None}

    # 2. Google (live API for groups)
    if email:
        try:
            from app.integrations.google.adapter import GoogleWorkspaceAdapter
            google = GoogleWorkspaceAdapter(db)
            accounts = await google.list_users(query=f"email={email}", max_results=1)
            if accounts:
                g_profile = accounts[0]
                try:
                    g_profile["groups"] = await google.get_user_groups(email)
                except Exception as e:
                    logger.warning(f"Could not fetch Google groups for {email}: {e}")
                    g_profile["groups"] = []
                profile["google"] = g_profile
        except Exception as e:
            logger.warning(f"Could not fetch Google profile for {email}: {e}")

    # 3. Paxton — from cache if matched
    if s.paxton_id:
        try:
            from app.integrations.paxton.adapter import PaxtonAdapter
            paxton = PaxtonAdapter(db)

            pax_cached = await db.execute(
                select(PaxtonUserCache).where(PaxtonUserCache.paxton_id == s.paxton_id)
            )
            pax_row = pax_cached.scalar_one_or_none()
            if pax_row:
                pax_user = {
                    "id": pax_row.paxton_id,
                    "first_name": pax_row.first_name, "last_name": pax_row.last_name,
                    "display_name": pax_row.display_name, "email": pax_row.email,
                    "department": pax_row.department, "department_id": pax_row.department_id,
                    "has_image": pax_row.has_image, "enabled": pax_row.enabled,
                    "pin": pax_row.pin, "activate_date": pax_row.activate_date,
                    "access_levels": json.loads(pax_row.access_levels) if pax_row.access_levels else [],
                }
                access_levels = await paxton.get_access_levels()
                all_levels = {l.get("id"): l.get("name", f"ID {l.get('id')}") for l in access_levels if isinstance(l, dict)}
                level_names = [all_levels.get(lid, f"ID {lid}") for lid in pax_user.get("access_levels", [])]
                photo = await paxton.get_photo_path(pax_user["id"])
                last_door = await paxton.get_last_door_event(pax_user["id"])
                timezones = await paxton.get_timezones()
                profile["paxton"] = {
                    **pax_user,
                    "access_level_names": level_names,
                    "photo": photo,
                    "last_door": last_door,
                }
                profile["paxton_access_levels"] = [
                    {"id": l.get("id"), "name": l.get("name", "")}
                    for l in access_levels if isinstance(l, dict)
                ]
                profile["paxton_timezones"] = timezones
        except Exception as e:
            logger.warning(f"Could not fetch Paxton profile: {e}")

    # 4. Teaching schedule
    if email:
        try:
            from app.modules.roster.models import StudentTeacher
            schedule_q = (
                select(
                    StudentTeacher.section_name,
                    StudentTeacher.school,
                    StudentTeacher.period,
                    func.count(StudentTeacher.student_id).label("student_count"),
                )
                .where(StudentTeacher.teacher_email == email)
                .group_by(StudentTeacher.section_name, StudentTeacher.school, StudentTeacher.period)
                .order_by(StudentTeacher.school, StudentTeacher.section_name)
            )
            schedule_result = await db.execute(schedule_q)
            sections = [
                {"section": row.section_name or "Unknown", "school": row.school or "",
                 "period": row.period or "", "students": row.student_count}
                for row in schedule_result.all()
            ]
            if sections:
                profile["schedule"] = sections
        except Exception as e:
            logger.warning(f"Could not fetch teaching schedule for {email}: {e}")

    # 5. Live UCM phone status (30s cached poll) if extension matched
    if s.extension:
        try:
            from app.modules.settings.repository import get_setting_value as _gsv2
            ucm_url = await _gsv2(db, "grandstream", "url")
            if ucm_url:
                from app.integrations.grandstream.adapter import ucm_session
                async with ucm_session(db) as ucm:
                    phone_dir = await ucm.get_extension_directory()
                    match = next(
                        (p for p in phone_dir if p.get("extension") == s.extension), None
                    )
                    if match:
                        profile["extension"] = match["extension"]
                        profile["phone_registered"] = match["registered"]
                        profile["phone_status"] = match["status"]
                        profile["phone_ip"] = match.get("ip", "")
                        profile["phone_model"] = match.get("model", "")
        except Exception as e:
            logger.warning(f"UCM profile lookup failed: {e}")

        # Fall back to static data if live poll didn't populate
        if "extension" not in profile:
            profile["extension"] = s.extension
            profile["phone_registered"] = s.phone_registered
            profile["phone_status"] = "registered" if s.phone_registered else "unregistered"
            profile["phone_ip"] = s.phone_ip or ""
            profile["phone_model"] = s.phone_model or ""

        # VM-to-email fields — live UCM (via getUser) so a change made
        # elsewhere shows up immediately, falling back to the phone
        # cache for offline responsiveness. UCM has both the email
        # destination and the yes/no delivery toggle.
        try:
            from app.integrations.grandstream.adapter import ucm_session as _us
            async with _us(db) as ucm:
                udata = await ucm._api("getUser", {"user_name": s.extension})
                u = (udata or {}).get("user_name", {}) or {}
                profile["phone_email"] = (u.get("email") or "").strip()
                profile["phone_email_to_user"] = u.get("email_to_user") or "no"
        except Exception:
            # Fall through to cache — /phones sync populates email there.
            try:
                from app.modules.phones.models import PhoneExtensionCache
                cached = (await db.execute(
                    select(PhoneExtensionCache).where(PhoneExtensionCache.extension == s.extension)
                )).scalar_one_or_none()
                if cached:
                    profile["phone_email"] = cached.email or ""
                    # The cache doesn't carry the toggle; leave it
                    # unknown so the UI defaults to a safe reading.
                    profile.setdefault("phone_email_to_user", "no")
            except Exception:
                pass

    # 6. HR cache (cert number, classification, tab, notes) — helpful
    #    on the teacher profile. Match by email first; fall back to
    #    "First Last" because HR's email column is often blank/formula
    #    on new hires (Laiken Combs et al.).
    try:
        from app.modules.staff.models import HRStaffCache
        hr_row = None
        if email:
            hr_row = (await db.execute(
                select(HRStaffCache).where(func.lower(HRStaffCache.email) == email)
            )).scalar_one_or_none()
        if hr_row is None:
            full = f"{(s.first_name or '').strip()} {(s.last_name or '').strip()}".strip()
            if full:
                hr_row = (await db.execute(
                    select(HRStaffCache).where(func.lower(HRStaffCache.name) == full.lower())
                )).scalar_one_or_none()
        if hr_row is not None:
            profile["hr"] = {
                "name": hr_row.name,
                "position": hr_row.position,
                "school": hr_row.school,
                "classification": hr_row.classification,
                "tab_source": hr_row.tab_source,
                "notes": hr_row.notes,
                "cert_number": hr_row.cert_number,
            }
    except Exception as e:
        logger.warning(f"HR cache lookup failed for {username}: {e}")

    # Check for active profile photo
    _active_name = _resolve_active_photo(username)
    if _active_name:
        profile["ad"]["photo"] = f"/api/staff/media/{_active_name}{_photo_cache_buster(_active_name)}"

    return profile


# ── API: Profile Photo Upload ─────────────────────────────────────────────

@router.post("/api/staff/profile/{username}/google-ou")
async def change_staff_google_ou(
    username: str,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Direct Google OU override for a staff account.

    The `/edit` endpoint moves OU via a building-profile lookup —
    correct for role-driven placement but wrong when the operator
    needs to override to a specific path (e.g. rescue an account
    from '/' or '/Archived Accounts' after a rehire).

    Body: {"ou_path": "/Users-Classified-Staff/PES"} — absolute
    path, must start with '/'. Empty or missing → 400.
    """
    body = await request.json()
    ou_path = (body.get("ou_path") or "").strip()
    if not ou_path or not ou_path.startswith("/"):
        raise HTTPException(status_code=400, detail="ou_path must be an absolute path starting with '/'")

    # Resolve the email — username may be sAMAccountName or email prefix
    from app.modules.staff.models import StaffReconciliation
    recon = (await db.execute(
        select(StaffReconciliation).where(StaffReconciliation.username == username)
    )).scalar_one_or_none()
    if not recon or not recon.email:
        raise HTTPException(status_code=404, detail=f"No staff row for {username}")

    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    google = GoogleWorkspaceAdapter(db)
    result = await google.move_user_ou(recon.email, ou_path)
    if not result.success:
        raise HTTPException(status_code=502, detail=f"Google move failed: {result.error}")

    await log_action(
        db, actor=user.email, action="staff.google.ou_move",
        module="staff", target=recon.email,
        details=f"→ {ou_path}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "email": recon.email, "new_ou": ou_path}


@router.post("/api/staff/profile/{username}/photo")
async def upload_profile_photo(
    username: str,
    request: Request,
    photo: UploadFile = File(...),
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Upload a staff photo. Nexus is the system of record — the file
    goes to disk + a staff_photos row carries the metadata. A future
    Paxton push-sync worker (if a district adds Paxton) can read from
    staff_photos; the upload path itself has no Paxton coupling.

    Still syncs to Google Workspace if that integration is configured
    and the staff row has an email — Google is where the photo surfaces
    in Gmail/Meet/Calendar, so it's worth the opportunistic push."""
    from app.modules.staff import photo_service

    # Validate file
    ext = os.path.splitext(photo.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type not allowed: {ext}")
    content = await photo.read()

    try:
        result = await photo_service.upload_photo(
            db,
            username=username,
            raw_bytes=content,
            uploaded_by=user.email or "unknown",
            source="upload",
            set_active=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"Photo upload failed for {username}: {e}")
        raise HTTPException(status_code=400, detail="Could not process image")

    # Opportunistic Google Workspace photo sync — if the integration is
    # configured and the staff row has an email, push there too. Any
    # error degrades gracefully (we have the authoritative copy in DB).
    google_synced = False
    try:
        from app.modules.staff.models import StaffReconciliation
        from sqlalchemy import func
        recon = await db.execute(
            select(StaffReconciliation).where(func.lower(StaffReconciliation.username) == username.lower())
        )
        staff = recon.scalar_one_or_none()
        if staff and staff.email:
            try:
                from app.integrations.google.adapter import GoogleWorkspaceAdapter
                await GoogleWorkspaceAdapter(db).update_user_photo(staff.email, result["bytes"])
                google_synced = True
            except Exception as e:
                logger.warning(f"Google photo sync failed for {username}: {e}")
    except ModuleNotFoundError:
        pass  # Google integration not present in this fork

    await log_action(db, actor=user.email, action="staff.profile.photo",
                     module="staff", target=username,
                     ip_address=request.client.host if request.client else None)
    await db.commit()

    return {
        "ok": True,
        "id": result["id"],
        "filename": result["filename"],
        "google_synced": google_synced,
    }


@router.get("/api/staff/profile/{username}/photos")
async def list_profile_photos(
    username: str,
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """List all saved photos for a staff member, newest first."""
    from app.modules.staff import photo_service

    rows = await photo_service.list_photos(db, username=username)
    photos = []
    for r in rows:
        fname = r["filename"]
        photos.append({
            "id": r["id"],
            "filename": fname,
            "url": f"/api/staff/media/{fname}{photo_service.photo_cache_buster(fname)}",
            "timestamp": r["uploaded_at"].strftime("%Y%m%d_%H%M%S") if r["uploaded_at"] else "",
            "uploaded_by": r["uploaded_by"],
            "active": r["is_active"],
            "source": r["source"],
        })
    return {"photos": photos}


@router.post("/api/staff/profile/{username}/photos/set-active")
async def set_active_photo(
    username: str,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Set an existing photo as the active profile photo.

    Accepts either {"photo_id": N} (preferred — stable DB primary key)
    or {"filename": "..."} (legacy — resolves to the matching row's id).
    Opportunistically pushes the activated photo to Google Workspace."""
    from app.modules.staff import photo_service
    body = await request.json()

    photo_id = body.get("photo_id")
    if photo_id is None:
        # Legacy clients send filename
        filename = body.get("filename", "")
        if not filename:
            raise HTTPException(status_code=400, detail="photo_id or filename required")
        from sqlalchemy import text as _text
        row = (await db.execute(
            _text("""
                SELECT id FROM staff_photos
                WHERE filename = :f AND lower(staff_username) = lower(:u)
            """),
            {"f": filename, "u": username},
        )).first()
        if not row:
            raise HTTPException(status_code=404, detail="Photo not found")
        photo_id = row[0]

    result = await photo_service.set_active(db, username=username, photo_id=int(photo_id))
    if not result:
        raise HTTPException(status_code=404, detail="Photo not found for this user")

    # Opportunistic Google sync.
    google_synced = False
    try:
        with open(os.path.join(photo_service.MEDIA_DIR, result["filename"]), "rb") as f:
            jpeg_bytes = f.read()
        from app.modules.staff.models import StaffReconciliation
        from sqlalchemy import func
        recon = await db.execute(
            select(StaffReconciliation).where(func.lower(StaffReconciliation.username) == username.lower())
        )
        staff = recon.scalar_one_or_none()
        if staff and staff.email:
            try:
                from app.integrations.google.adapter import GoogleWorkspaceAdapter
                await GoogleWorkspaceAdapter(db).update_user_photo(staff.email, jpeg_bytes)
                google_synced = True
            except Exception as e:
                logger.warning(f"Google photo sync failed: {e}")
    except (ModuleNotFoundError, FileNotFoundError):
        pass

    await log_action(db, actor=user.email, action="staff.profile.photo.set_active",
                     module="staff", target=f"{username}/{result['filename']}",
                     ip_address=request.client.host if request.client else None)
    await db.commit()

    return {"ok": True, "google_synced": google_synced}


@router.delete("/api/staff/profile/{username}/photos/{photo_id}")
async def delete_profile_photo(
    username: str,
    photo_id: int,
    request: Request,
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a photo. Users may delete their own uploads; admins may
    delete any. Returns 403 if the actor is neither the uploader nor
    an admin."""
    from app.modules.staff import photo_service

    # "Admin" for photo deletion = has the elevated provision permission.
    # Mirrors the gating on upload/set-active so photo lifecycle is
    # consistent with the rest of the staff module.
    from app.policies.engine import get_user_permissions, check_permission
    perms = await get_user_permissions(db, user.id)
    actor_is_admin = check_permission(perms, "staff.provision.execute")

    deleted = await photo_service.delete_photo(
        db,
        username=username,
        photo_id=photo_id,
        actor_email=user.email or "",
        actor_is_admin=actor_is_admin,
    )
    if not deleted:
        raise HTTPException(status_code=403, detail="Photo not found or not permitted")

    await log_action(db, actor=user.email, action="staff.profile.photo.delete",
                     module="staff", target=f"{username}/{photo_id}",
                     ip_address=request.client.host if request.client else None)
    await db.commit()
    return {"ok": True}


# ── API: Reconciliation Refresh ──────────────────────────────────────────

@router.post("/api/staff/reconciliation/refresh")
async def refresh_reconciliation(
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Enqueue a staff reconciliation job (on-demand refresh)."""
    from arq import create_pool
    from arq.connections import RedisSettings
    from app.config import get_settings

    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    window = int(datetime.now(timezone.utc).timestamp() // 30)
    await redis.enqueue_job("run_staff_reconciliation", _job_id=f"reconciliation:{window}")

    await log_action(
        db, actor=user.email, action="staff.reconciliation.refresh",
        module="staff", target="manual",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "message": "Reconciliation job enqueued"}


# ── API: Staff Counts ────────────────────────────────────────────────────

@router.get("/api/staff/counts")
async def staff_counts(
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """Staff counts by building — matches what the directory shows."""
    from app.modules.staff.models import StaffDirectoryEntry, StaffIgnore

    # Get ignored usernames
    ignored_result = await db.execute(
        select(StaffIgnore.username).where(StaffIgnore.restored_at == None)  # noqa: E711
    )
    ignored_usernames = {r[0].lower() for r in ignored_result.all() if r[0]}

    # Count from the directory table (Google source of truth)
    all_staff = await db.execute(
        select(StaffDirectoryEntry.email, StaffDirectoryEntry.building)
        .where(StaffDirectoryEntry.status == "active")
    )
    counts = {"total": 0, "ignored": len(ignored_usernames)}
    for email, building in all_staff.all():
        prefix = email.split("@")[0].lower() if email else ""
        if prefix in ignored_usernames:
            continue
        b = building or "other"
        counts[b] = counts.get(b, 0) + 1
        counts["total"] = counts.get("total", 0) + 1

    return counts


# ── API: Phone Extension Management ─────────────────────────────────────

@router.get("/api/staff/extension/{ext}")
async def get_extension_detail(
    ext: str,
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """Get full extension details from UCM (singleton session — safe)."""
    from app.integrations.grandstream.adapter import ucm_session
    async with ucm_session(db) as ucm:
        data = await ucm.get_extension(ext)
        all_ext = await ucm.list_extensions()
        for e in all_ext:
            if e["extension"] == ext:
                data["status"] = e["status"]
                data["registered"] = e["registered"]
                data["ip"] = e["ip"]
                data["model"] = e["model"]
                break
        return data


@router.post("/api/staff/extension/{ext}")
async def update_extension_name(
    ext: str,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """
    Assign / rename / update an extension on the UCM.

    Body: {first_name?, last_name?, email?, email_to_user?}

    All fields optional — at least one must be present or 400. Each
    field that is present is pushed to the UCM's User Management
    record for the extension.

    ``email`` — VM-to-email destination address.
    ``email_to_user`` — "yes"|"no" toggle for whether the UCM actually
    mails voicemails to ``email``. If omitted but ``email`` is set,
    defaults to "yes" (assigning a person to an extension implies
    they want the voicemails delivered).

    When ``email`` is omitted but ``first_name + last_name`` are
    provided and match exactly one staff_reconciliation row, that
    row's email auto-fills — keeps the Assign button one-click.
    """
    body = await request.json()
    first_name = body.get("first_name", "").strip()
    last_name = body.get("last_name", "").strip()
    email = (body.get("email") or "").strip()
    # Accept explicit "yes"/"no"; None means "let the server decide
    # based on whether an email is being set."
    email_to_user_raw = body.get("email_to_user")
    email_to_user = None
    if email_to_user_raw is not None:
        v = str(email_to_user_raw).strip().lower()
        if v in ("yes", "true", "1", "on"):
            email_to_user = "yes"
        elif v in ("no", "false", "0", "off", ""):
            email_to_user = "no"

    if not first_name and not last_name and not email and email_to_user is None:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of first_name, last_name, email, or email_to_user",
        )

    # If the caller didn't provide an email but did provide a name,
    # auto-fill from staff_reconciliation. Only fires on exact name
    # match with a single candidate — otherwise silent skip so we
    # don't mail voicemails to the wrong person.
    if not email and (first_name or last_name):
        try:
            from app.modules.staff.models import StaffReconciliation
            match = (await db.execute(
                select(StaffReconciliation).where(
                    func.lower(StaffReconciliation.first_name) == first_name.lower(),
                    func.lower(StaffReconciliation.last_name) == last_name.lower(),
                )
            )).scalars().all()
            if len(match) == 1 and match[0].email:
                email = match[0].email.strip()
        except Exception as e:
            logger.warning(f"Email auto-resolve for ext {ext} failed: {e}")

    # Fall-through default for email_to_user: turn ON when we have an
    # email to send to, leave alone otherwise. Flipping the flag with
    # no destination is worse than nothing.
    if email_to_user is None and email:
        email_to_user = "yes"

    from app.integrations.grandstream.adapter import ucm_session, _state as _ucm_state2

    # Verify extension exists before trying to update
    cached = _ucm_state2.extensions_cache if _ucm_state2.extensions_cache else []
    if cached and not any(e["extension"] == ext for e in cached):
        raise HTTPException(status_code=404, detail=f"Extension {ext} not found on the phone system")

    async with ucm_session(db) as ucm:
        try:
            result = await ucm.update_user(
                ext,
                first_name=first_name or None,
                last_name=last_name or None,
                email=email or None,
                email_to_user=email_to_user,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=f"Failed to update extension {ext}: {str(e)[:100]}")
    # Invalidate the in-memory cache and update the DB phone cache
    _ucm_state2.extensions_cache_time = 0
    try:
        from app.modules.phones.models import PhoneExtensionCache
        cached_ext = await db.execute(
            select(PhoneExtensionCache).where(PhoneExtensionCache.extension == ext)
        )
        row = cached_ext.scalar_one_or_none()
        if row:
            if first_name or last_name:
                row.caller_id_name = f"{first_name} {last_name}".strip()
            if email:
                row.email = email
    except Exception:
        pass  # Phone cache table may not exist yet

    await log_action(
        db,
        actor=user.email,
        action="staff.extension.update_name",
        module="staff",
        target=f"ext:{ext}",
        details=json.dumps({
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "email_to_user": email_to_user,
        })[:500],
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {**result, "email_pushed": bool(email), "email_to_user": email_to_user}


# ── API: Update Paxton Access ────────────────────────────────────────────

@router.post("/api/staff/update-paxton-access")
async def update_paxton_access(
    body: UpdatePaxtonAccessRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Update a Paxton user's access level."""
    from app.integrations.paxton.adapter import PaxtonAdapter

    paxton = PaxtonAdapter(db)
    try:
        result = await paxton.update_user_access(body.paxton_id, body.access_level_id)
        if not result.success:
            raise HTTPException(status_code=500, detail=result.error)
        await log_action(
            db, actor=user.email, action="staff.paxton.update_access",
            module="staff",
            target=f"Paxton ID {body.paxton_id}",
            details=f"access_level_id={body.access_level_id}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


# ── API: Google Groups ───────────────────────────────────────────────────

@router.post("/api/staff/add-google-group")
async def add_google_group(
    body: GoogleGroupRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Add a user to a Google group."""
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    google = GoogleWorkspaceAdapter(db)
    try:
        result = await google.add_to_group(body.email, body.group_email)
        if not result.added:
            raise HTTPException(status_code=500, detail=result.error or "Failed to add to group")
        await log_action(
            db, actor=user.email, action="staff.google.add_group",
            module="staff", target=body.email,
            details=f"group={body.group_email}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


@router.post("/api/staff/remove-google-group")
async def remove_google_group(
    body: GoogleGroupRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Remove a user from a Google group."""
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    import asyncio

    google = GoogleWorkspaceAdapter(db)
    try:
        service = await google._get_service()

        def _remove():
            service.members().delete(groupKey=body.group_email, memberKey=body.email).execute()

        await asyncio.to_thread(_remove)
        await log_action(
            db, actor=user.email, action="staff.google.remove_group",
            module="staff", target=body.email,
            details=f"group={body.group_email}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── API: Rename Primary Email ─────────────────────────────────────────────


@router.get("/api/staff/rename-email-preview")
async def rename_email_preview(
    current_email: str,
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Compute what the rename endpoint WOULD set the new primary to,
    without touching anything. Used by the UI's confirmation dialog
    so IT can eyeball the target before clicking through.
    """
    from app.modules.staff.models import StaffReconciliation, HRStaffCache
    from sqlalchemy import select
    import re as _re

    cur = (current_email or "").strip().lower()
    if not cur or "@" not in cur:
        raise HTTPException(status_code=400, detail="current_email required")
    domain = cur.split("@", 1)[1]

    recon = (await db.execute(
        select(StaffReconciliation).where(StaffReconciliation.email == cur)
    )).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail="Account not in directory")

    hr = None
    if recon.hr_email:
        hr = (await db.execute(
            select(HRStaffCache).where(HRStaffCache.email == recon.hr_email.lower())
        )).scalar_one_or_none()
    if hr is None:
        rf = (recon.first_name or "").strip().lower()
        rl = (recon.last_name or "").strip().lower()
        hrs = (await db.execute(select(HRStaffCache))).scalars().all()
        for h in hrs:
            parts = (h.name or "").strip().split(None, 1)
            if len(parts) == 2 and parts[0].lower() == rf and parts[1].lower() == rl:
                hr = h
                break
    if hr is None:
        raise HTTPException(status_code=404, detail="No matching HR row")

    parts = (hr.name or "").strip().split(None, 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Incomplete HR name")
    hf = _re.sub(r"[^a-z]", "", parts[0].lower())
    hl = _re.sub(r"[^a-z]", "", parts[1].lower())
    if not hf or not hl:
        raise HTTPException(status_code=400, detail="Incomplete HR name")
    return {"new_email": f"{hf}.{hl}@{domain}", "hr_name": hr.name, "hr_notes": hr.notes}


class RenameEmailRequest(PydanticBase):
    """Body for /api/staff/rename-email."""
    current_email: str
    new_email: str | None = None  # If omitted, compute from HR name


@router.post("/api/staff/rename-email")
async def rename_primary_email(
    body: RenameEmailRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """
    Rename a staff member's primary Google email, typically after a
    legal-name change HR has flagged via a Née/Formerly note.

    Steps:
      1. Compute the target email (if not supplied) from the HR row's
         current first.last on the same domain as the existing account.
      2. Rename the Google primary. The Admin SDK automatically adds
         the old address as an alias so inbound mail keeps working.
      3. Best-effort update the Paxton cardholder's email custom
         field if the account is matched.
      4. Best-effort update AD's ``mail`` attribute if the account is
         matched. AD ``sAMAccountName`` is intentionally NOT renamed —
         the login name stays the same, only the email changes.
      5. Enqueue a staff directory + reconciliation sync so the UI
         reflects the new state on next refresh.
      6. Audit log the action and per-system outcomes.

    Returns ``{status, google, paxton, ad, new_email}`` where each
    system field is ``"ok" | "skipped" | "failed: <reason>"``.
    """
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.modules.staff.models import (
        StaffReconciliation, HRStaffCache, ADUserCache, PaxtonUserCache,
    )
    from sqlalchemy import select
    import re as _re

    current = (body.current_email or "").strip().lower()
    if not current or "@" not in current:
        raise HTTPException(status_code=400, detail="current_email required")

    domain = current.split("@", 1)[1]

    # Resolve the target email. If the caller didn't pin one, look up
    # the HR row (via staff_reconciliation.hr_email or name match) and
    # compute <first>.<last>@<domain> from HR's current name.
    new_email = (body.new_email or "").strip().lower()
    if not new_email:
        recon = (await db.execute(
            select(StaffReconciliation).where(StaffReconciliation.email == current)
        )).scalar_one_or_none()
        if not recon:
            raise HTTPException(
                status_code=404, detail="Account not found in directory",
            )
        hr = None
        # Prefer the captured hr_email if reconciliation recorded one.
        if recon.hr_email:
            hr = (await db.execute(
                select(HRStaffCache).where(HRStaffCache.email == recon.hr_email.lower())
            )).scalar_one_or_none()
        # Fallback — match by canonical first + last against hr_staff_cache.
        if hr is None:
            rf = (recon.first_name or "").strip().lower()
            rl = (recon.last_name or "").strip().lower()
            hrs = (await db.execute(select(HRStaffCache))).scalars().all()
            for h in hrs:
                parts = (h.name or "").strip().split(None, 1)
                if len(parts) == 2 and parts[0].lower() == rf and parts[1].lower() == rl:
                    hr = h
                    break
        if hr is None:
            raise HTTPException(
                status_code=404,
                detail="No HR row found to derive the new email from",
            )
        parts = (hr.name or "").strip().split(None, 1)
        if len(parts) != 2:
            raise HTTPException(status_code=400, detail="HR name is incomplete")
        hf = _re.sub(r"[^a-z]", "", parts[0].lower())
        hl = _re.sub(r"[^a-z]", "", parts[1].lower())
        if not hf or not hl:
            raise HTTPException(status_code=400, detail="HR name is incomplete")
        new_email = f"{hf}.{hl}@{domain}"

    if new_email == current:
        raise HTTPException(
            status_code=400,
            detail=f"New email {new_email} is identical to current",
        )
    if new_email.split("@", 1)[1] != domain:
        raise HTTPException(
            status_code=400,
            detail="New email must be on the same domain",
        )

    steps = {"google": "skipped", "paxton": "skipped", "ad": "skipped"}

    # Google rename — the heavy step. If this fails, abort before
    # touching downstream systems so we don't end up half-migrated.
    google = GoogleWorkspaceAdapter(db)
    g_result = await google.rename_account(current, new_email)
    if not g_result.success:
        steps["google"] = f"failed: {g_result.error}"
        await log_action(
            db, actor=user.email, action="staff.rename_email.failed",
            module="staff", target=current,
            details=f"new={new_email}; {g_result.error}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Google rename failed: {g_result.error}")
    steps["google"] = "ok"

    # Paxton email field — best effort. Look up cardholder by current
    # email since that's what the cache still has.
    try:
        pax_row = (await db.execute(
            select(PaxtonUserCache).where(PaxtonUserCache.email == current)
        )).scalar_one_or_none()
        if pax_row and pax_row.paxton_id:
            from app.integrations.paxton.adapter import PaxtonAdapter
            pax = PaxtonAdapter(db)
            raw = await pax._api_call("GET", f"users/{pax_row.paxton_id}")
            if raw:
                email_fid, _ = await pax._get_custom_field_ids()
                cfs = raw.get("customFields", [])
                found = False
                for cf in cfs:
                    if cf.get("id") == email_fid:
                        cf["value"] = new_email
                        found = True
                        break
                if not found:
                    cfs.append({"id": email_fid, "value": new_email})
                await pax._api_call(
                    "PUT", f"users/{pax_row.paxton_id}",
                    {
                        "id": pax_row.paxton_id,
                        "firstName": raw.get("firstName", ""),
                        "lastName": raw.get("lastName", ""),
                        "customFields": cfs,
                    },
                )
                steps["paxton"] = "ok"
    except Exception as pe:
        logger.warning(f"Paxton email update failed during rename: {pe}")
        steps["paxton"] = f"failed: {str(pe)[:80]}"

    # AD mail attribute — best effort. sAMAccountName is NOT touched.
    try:
        ad_row = (await db.execute(
            select(ADUserCache).where(ADUserCache.email == current)
        )).scalar_one_or_none()
        if ad_row and ad_row.username:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad = ActiveDirectoryAdapter(db)
            ar = await ad.update_mail(ad_row.username, new_email) if hasattr(ad, "update_mail") else None
            if ar is None:
                # No update_mail helper yet — skip with a log note.
                steps["ad"] = "skipped: adapter has no update_mail()"
            elif getattr(ar, "success", False):
                steps["ad"] = "ok"
            else:
                steps["ad"] = f"failed: {getattr(ar, 'error', 'unknown')}"
    except Exception as ae:
        logger.warning(f"AD mail update failed during rename: {ae}")
        steps["ad"] = f"failed: {str(ae)[:80]}"

    await log_action(
        db, actor=user.email, action="staff.rename_email",
        module="staff", target=f"{current} → {new_email}",
        details=json.dumps(steps),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    # Enqueue refresh so the directory reflects the new state.
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings as _gs
        redis = await create_pool(RedisSettings.from_dsn(_gs().redis_url))
        window = int(datetime.now(timezone.utc).timestamp() // 30)
        await redis.enqueue_job(
            "sync_staff_directory",
            _job_id=f"sync:rename:{window}",
        )
        await redis.enqueue_job(
            "run_staff_reconciliation",
            _job_id=f"reconciliation:rename:{window}",
        )
    except Exception as re_err:
        logger.warning(f"Rename post-sync enqueue failed: {re_err}")

    return {
        "status": "ok",
        "new_email": new_email,
        "google": steps["google"],
        "paxton": steps["paxton"],
        "ad": steps["ad"],
    }


# ── API: Edit Profile ────────────────────────────────────────────────────
#
# Profile edit orchestrator. Accepts partial updates for name/title/
# building/room/extension and cascades to Google + AD + Paxton + UCM.
# Every system's result is captured independently — a failure on one
# does not abort the others (except that AD login rename is deferred
# to the end because it changes the lookup key).
#
# Ordering rationale:
#   1. Email rename first (if requested). Google auto-adds an alias for
#      the old address so mail continues to flow.
#   2. Name/title updates via the standing lookup keys.
#   3. Building change: Google OU move + AD OU move + Paxton department.
#   4. Extension display name update (UCM).
#   5. AD sAMAccountName rename LAST (if requested). It rewrites the
#      login every earlier step used to find the user, so nothing else
#      should be attempted against the old sAMAccountName afterwards.

@router.post("/api/staff/profile/{username}/edit")
async def edit_staff_profile(
    username: str,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """
    Edit a staff member's profile across Google, AD, Paxton, and UCM.

    Body fields (all optional — omit or send null to leave unchanged):
      first_name, last_name, display_name, title, building

    Not editable here:
      - room: source-of-truth is the room_roster Google Sheet; a write
        here would be overwritten on the next sync.
      - preferred_name: no storage layer on reconciliation. Edit
        through the queue-entry surface at provisioning time.
      - extension (reassignment): goes through the phones module.
        Extension display name auto-updates when first/last change.

    Systems (opt-in flags, default true if the underlying account
    exists): google, ad, paxton, phone

    Rename options:
      rename_email  — if last_name changed, also rename the primary
                      email. Uses <first>.<last>@<domain> unless
                      ``new_email`` is supplied explicitly.
      rename_ad_login — if last_name changed, also rename the AD
                        sAMAccountName. Uses first-initial + last
                        unless ``new_ad_username`` is supplied.

    Returns per-system results as {system: "ok" | "skipped" | "failed: <reason>"}.
    """
    from sqlalchemy import select, text as _text
    from app.modules.staff.models import (
        StaffReconciliation, ADUserCache, PaxtonUserCache,
    )
    import re as _re

    body = await request.json()

    recon = (await db.execute(
        select(StaffReconciliation).where(
            func.lower(StaffReconciliation.username) == username.lower()
        )
    )).scalar_one_or_none()
    if not recon:
        raise HTTPException(status_code=404, detail=f"User not found: {username}")

    # Normalize inputs — treat empty string same as "no change" so the
    # caller can send a blank field without accidentally clearing an
    # attribute. Explicit clears aren't supported here; those go
    # through the disable/deprovision flow instead.
    def _norm(v):
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    new_first = _norm(body.get("first_name"))
    new_last = _norm(body.get("last_name"))
    new_display = _norm(body.get("display_name"))
    new_title = _norm(body.get("title"))
    new_building = _norm(body.get("building"))

    systems = body.get("systems") or {}
    do_google = bool(systems.get("google", True))
    do_ad = bool(systems.get("ad", True))
    do_paxton = bool(systems.get("paxton", True))
    do_phone = bool(systems.get("phone", True))

    rename_email_flag = bool(body.get("rename_email"))
    rename_ad_login_flag = bool(body.get("rename_ad_login"))
    supplied_new_email = _norm(body.get("new_email"))
    supplied_new_ad_username = _norm(body.get("new_ad_username"))

    current_email = (recon.email or "").strip().lower()
    current_ad = (recon.ad_username or "").strip()
    current_first = (recon.first_name or "").strip()
    current_last = (recon.last_name or "").strip()

    last_name_changed = bool(new_last) and new_last.lower() != current_last.lower()

    steps: dict[str, str] = {}
    diff: dict[str, dict] = {}

    def _record_diff(field: str, old, new):
        if new is None:
            return
        if (old or "") == (new or ""):
            return
        diff[field] = {"old": old or "", "new": new}

    _record_diff("first_name", current_first, new_first)
    _record_diff("last_name", current_last, new_last)
    _record_diff("display_name", (recon.display_name or ""), new_display)
    _record_diff("title", (recon.title or ""), new_title)
    _record_diff("building", (recon.building or ""), new_building)

    # Nothing to do? Fail loud so the operator doesn't think they saved
    # a change that never fired.
    if not diff and not rename_email_flag and not rename_ad_login_flag:
        raise HTTPException(status_code=400, detail="No changes supplied")

    # ── Step 1: Email rename ────────────────────────────────────────
    active_email = current_email
    if rename_email_flag and last_name_changed and current_email and do_google:
        if supplied_new_email:
            target_email = supplied_new_email.lower()
        else:
            domain = current_email.split("@", 1)[1] if "@" in current_email else ""
            hf = _re.sub(r"[^a-z]", "", (new_first or current_first).lower())
            hl = _re.sub(r"[^a-z]", "", new_last.lower())
            target_email = f"{hf}.{hl}@{domain}" if hf and hl and domain else ""

        if not target_email or target_email == current_email:
            steps["email_rename"] = "skipped: unable to compute new email"
        else:
            try:
                from app.integrations.google.adapter import GoogleWorkspaceAdapter
                google_r = GoogleWorkspaceAdapter(db)
                gr = await google_r.rename_account(current_email, target_email)
                if gr.success:
                    steps["email_rename"] = f"ok: {current_email} -> {target_email}"
                    active_email = target_email
                else:
                    steps["email_rename"] = f"failed: {gr.error}"
            except Exception as e:
                steps["email_rename"] = f"failed: {str(e)[:120]}"

    # ── Step 2: Google name update ──────────────────────────────────
    if do_google and (new_first is not None or new_last is not None) and active_email:
        try:
            from app.integrations.google.adapter import GoogleWorkspaceAdapter
            google2 = GoogleWorkspaceAdapter(db)
            gr2 = await google2.update_names(
                active_email,
                first_name=new_first,
                last_name=new_last,
            )
            steps["google_names"] = "ok" if gr2.success else f"failed: {gr2.error}"
        except Exception as e:
            steps["google_names"] = f"failed: {str(e)[:120]}"

    # ── Step 3: AD attribute updates ────────────────────────────────
    ad_pending = (
        do_ad and current_ad and (
            new_first is not None or new_last is not None or new_display is not None
        )
    )
    if ad_pending:
        try:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad_c = ActiveDirectoryAdapter(db)
            ar = await ad_c.update_names(
                current_ad,
                first_name=new_first,
                last_name=new_last,
                display_name=new_display,
            )
            steps["ad_names"] = "ok" if ar.success else f"failed: {ar.error}"
        except Exception as e:
            steps["ad_names"] = f"failed: {str(e)[:120]}"

    if do_ad and current_ad and new_title is not None:
        try:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad_t = ActiveDirectoryAdapter(db)
            tr = await ad_t.update_title(current_ad, new_title)
            steps["ad_title"] = "ok" if tr.success else f"failed: {tr.error}"
        except Exception as e:
            steps["ad_title"] = f"failed: {str(e)[:120]}"

    # AD mail attribute follows the email rename if it happened.
    if do_ad and current_ad and active_email != current_email:
        try:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad_m = ActiveDirectoryAdapter(db)
            mr = await ad_m.update_mail(current_ad, active_email)
            steps["ad_mail"] = "ok" if mr.success else f"failed: {mr.error}"
        except Exception as e:
            steps["ad_mail"] = f"failed: {str(e)[:120]}"

    # ── Step 4: Paxton name + email ─────────────────────────────────
    paxton_id = None
    if recon.paxton_id:
        paxton_id = recon.paxton_id
    if do_paxton and paxton_id and (new_first is not None or new_last is not None):
        try:
            from app.integrations.paxton.adapter import PaxtonAdapter
            pax = PaxtonAdapter(db)
            pr = await pax.update_names(paxton_id, first_name=new_first, last_name=new_last)
            steps["paxton_names"] = "ok" if pr.success else f"failed: {pr.error}"
        except Exception as e:
            steps["paxton_names"] = f"failed: {str(e)[:120]}"

    if do_paxton and paxton_id and active_email != current_email:
        try:
            pax_row = (await db.execute(
                select(PaxtonUserCache).where(PaxtonUserCache.paxton_id == paxton_id)
            )).scalar_one_or_none()
            if pax_row:
                from app.integrations.paxton.adapter import PaxtonAdapter
                pax2 = PaxtonAdapter(db)
                raw = await pax2._api_call("GET", f"users/{paxton_id}")
                if raw:
                    email_fid, _ = await pax2._get_custom_field_ids()
                    cfs = raw.get("customFields", [])
                    found = False
                    for cf in cfs:
                        if cf.get("id") == email_fid:
                            cf["value"] = active_email
                            found = True
                            break
                    if not found:
                        cfs.append({"id": email_fid, "value": active_email})
                    await pax2._api_call(
                        "PUT", f"users/{paxton_id}",
                        {
                            "id": paxton_id,
                            "firstName": raw.get("firstName", ""),
                            "lastName": raw.get("lastName", ""),
                            "customFields": cfs,
                        },
                    )
                    steps["paxton_email"] = "ok"
        except Exception as e:
            steps["paxton_email"] = f"failed: {str(e)[:120]}"

    # ── Step 5: Building change (OU moves + department) ─────────────
    if new_building and (recon.building or "").upper() != new_building.upper():
        # Resolve the target OUs from the provisioning profile so a
        # building change lands the user in the same place they'd be
        # if provisioned fresh today.
        try:
            from app.modules.staff.provisioning_profiles import get_profile as _get_profile
            _role = (recon.role_type or "teacher").lower() if hasattr(recon, "role_type") else "teacher"
            profile = await _get_profile(db, new_building.upper(), _role)
            g_ou = (profile.get("google_ou") or "").strip()
            a_ou = (profile.get("ad_ou") or "").strip()
            pax_dept_id = profile.get("paxton_department_id")
        except Exception as e:
            logger.warning(f"Profile lookup for building change failed: {e}")
            profile, g_ou, a_ou, pax_dept_id = {}, "", "", None

        if do_google and g_ou and active_email:
            try:
                from app.integrations.google.adapter import GoogleWorkspaceAdapter
                google3 = GoogleWorkspaceAdapter(db)
                gm = await google3.move_user_ou(active_email, g_ou)
                steps["google_ou"] = "ok" if gm.success else f"failed: {gm.error}"
            except Exception as e:
                steps["google_ou"] = f"failed: {str(e)[:120]}"
        elif do_google:
            steps["google_ou"] = f"skipped: no google_ou in profile {new_building.upper()}/{_role}"

        if do_ad and a_ou and current_ad:
            try:
                from app.integrations.ad.adapter import ActiveDirectoryAdapter
                ad_o = ActiveDirectoryAdapter(db)
                ou_path = a_ou if a_ou.upper().startswith("OU=") else f"OU={a_ou}"
                am = await ad_o.move_user(current_ad, ou_path)
                steps["ad_ou"] = "ok" if am.success else f"failed: {am.error}"
            except Exception as e:
                steps["ad_ou"] = f"failed: {str(e)[:120]}"

        if do_paxton and paxton_id and pax_dept_id:
            try:
                from app.integrations.paxton.adapter import PaxtonAdapter
                pax_d = PaxtonAdapter(db)
                pdm = await pax_d.move_to_department(paxton_id, int(pax_dept_id))
                steps["paxton_department"] = "ok" if pdm.success else f"failed: {pdm.error}"
            except Exception as e:
                steps["paxton_department"] = f"failed: {str(e)[:120]}"

    # ── Step 6: Extension display name (name-only, same ext) ────────
    # Extension reassignment happens elsewhere. Here we just push the
    # new first/last through to the UCM so caller ID matches.
    if do_phone and recon.extension and (new_first is not None or new_last is not None):
        try:
            from app.integrations.grandstream.adapter import ucm_session
            async with ucm_session(db) as ucm:
                await ucm.update_user(
                    recon.extension,
                    first_name=(new_first if new_first is not None else current_first),
                    last_name=(new_last if new_last is not None else current_last),
                )
                steps["phone_names"] = "ok"
        except Exception as e:
            steps["phone_names"] = f"failed: {str(e)[:120]}"

    # ── Step 8: AD login rename LAST ────────────────────────────────
    # This changes the sAMAccountName every prior step used to find
    # the user. Doing it here avoids racing every other AD call.
    if rename_ad_login_flag and last_name_changed and current_ad and do_ad:
        if supplied_new_ad_username:
            target_ad = supplied_new_ad_username.lower()
        else:
            # Convention: first-initial + last, lowercase, alphanumeric only.
            fi = _re.sub(r"[^a-z]", "", (new_first or current_first).lower())[:1]
            ll = _re.sub(r"[^a-z]", "", new_last.lower())
            target_ad = f"{fi}{ll}" if fi and ll else ""
        if not target_ad or target_ad.lower() == current_ad.lower():
            steps["ad_login_rename"] = "skipped: unable to compute new login"
        else:
            try:
                from app.integrations.ad.adapter import ActiveDirectoryAdapter
                ad_r = ActiveDirectoryAdapter(db)
                new_display_for_cn = new_display or (
                    f"{new_first or current_first} {new_last}".strip()
                )
                arr = await ad_r.rename_account(current_ad, target_ad, new_display_for_cn)
                if arr.success:
                    steps["ad_login_rename"] = f"ok: {current_ad} -> {target_ad}"
                else:
                    steps["ad_login_rename"] = f"failed: {arr.error}"
            except Exception as e:
                steps["ad_login_rename"] = f"failed: {str(e)[:120]}"

    # ── Write local copy of the changes ────────────────────────────
    # The OU moves above push the state to Google/AD/Paxton, but the
    # reconciliation row (which drives the UI) was never touched.
    # Without this write, the profile keeps showing the OLD building
    # until the next full sync overwrites it — Tim's complaint 2026-09-24.
    # `staff_sync` will re-derive from the OU on the next tick and
    # confirm/correct these values.
    if diff:
        _new_building = new_building or recon.building
        _new_first = new_first or current_first
        _new_last = new_last or current_last
        recon.building = _new_building
        recon.first_name = _new_first
        recon.last_name = _new_last
        if new_display:
            recon.display_name = new_display
        if new_title:
            recon.title = new_title

        # Also write upstream `staff_directory` — the cache the sync
        # reads to build recon rows. Without this, the next sync
        # re-reads the stale building from the upstream row and
        # overwrites the recon row we just fixed.
        await db.execute(_text(
            "UPDATE staff_directory SET building = :b, first_name = :f, last_name = :l "
            "WHERE LOWER(email) = LOWER(:e)"
        ).bindparams(b=_new_building, f=_new_first, l=_new_last, e=current_email))

    # ── Audit + directory refresh ───────────────────────────────────
    await log_action(
        db, actor=user.email, action="staff.profile.edit",
        module="staff",
        target=f"{current_first} {current_last} ({current_email})",
        details=json.dumps({"diff": diff, "steps": steps}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings as _gs
        redis = await create_pool(RedisSettings.from_dsn(_gs().redis_url))
        window = int(datetime.now(timezone.utc).timestamp() // 30)
        await redis.enqueue_job("sync_staff_directory", _job_id=f"sync:edit:{username}:{window}")
        await redis.enqueue_job("run_staff_reconciliation", _job_id=f"reconciliation:edit:{username}:{window}")
    except Exception as e:
        logger.warning(f"Post-edit sync enqueue failed: {e}")

    ok = all(v.startswith("ok") or v.startswith("skipped") for v in steps.values())
    return {"status": "ok" if ok else "partial", "steps": steps, "diff": diff}


# ── API: Disable ─────────────────────────────────────────────────────────

@router.post("/api/staff/disable")
async def disable_staff(
    body: DisableRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Disable an AD account (and optionally Paxton/Google in future)."""
    from app.integrations.ad.adapter import ActiveDirectoryAdapter

    ad = ActiveDirectoryAdapter(db)
    ad_user = await ad.get_user(body.username)
    if not ad_user:
        raise HTTPException(status_code=404, detail=f"AD user not found: {body.username}")
    if not ad_user.get("enabled"):
        raise HTTPException(status_code=409, detail="Account is already disabled")

    try:
        from app.modules.settings.repository import get_setting_value

        result = await ad.disable_account(body.username)
        if not result.success:
            raise HTTPException(status_code=500, detail=f"Disable failed: {result.error}")

        # Move to configured deprovision OU
        deprovision_ou = await get_setting_value(db, "ad", "deprovision_ou")
        moved = False
        if deprovision_ou:
            base_dn = await get_setting_value(db, "ad", "base_dn") or ""
            target_dn = f"{deprovision_ou},{base_dn}" if base_dn and not deprovision_ou.endswith(base_dn) else deprovision_ou
            move_result = await ad.move_user(body.username, target_dn)
            moved = move_result.success

        await log_action(
            db, actor=user.email, action="staff.ad.disable",
            module="staff",
            target=f"{body.username} ({ad_user.get('display_name')})",
            details=f"Disabled{' + moved to ' + deprovision_ou if moved else ''}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok", "username": body.username, "display_name": ad_user.get("display_name"), "moved": moved}
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


# ── API: Deprovision Google & Paxton ──────────────────────────────────────

class DeprovisionGoogleRequest(PydanticBase):
    email: str

@router.post("/api/staff/deprovision-google")
async def deprovision_google(
    body: DeprovisionGoogleRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Suspend a Google Workspace account."""
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    google = GoogleWorkspaceAdapter(db)

    # Verify account exists and is active
    try:
        exists = await google.check_email_exists(body.email)
        if not exists:
            raise HTTPException(status_code=404, detail=f"Google account not found: {body.email}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google lookup failed: {str(e)[:100]}")

    try:
        from app.modules.settings.repository import get_setting_value

        # Suspend the account
        result = await google.suspend_account(body.email)
        if not result.success:
            raise HTTPException(status_code=500, detail=f"Suspend failed: {result.error}")

        # Move to configured deprovision OU
        archive_ou = await get_setting_value(db, "google", "deprovision_ou") or "/Archived Accounts"
        move_result = await google.move_to_ou(body.email, archive_ou)
        moved = move_result.success

        await log_action(
            db, actor=user.email, action="staff.google.suspend",
            module="staff", target=body.email,
            details=f"Suspended and {'moved to ' + archive_ou if moved else 'OU move failed: ' + (move_result.error or '')}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok", "email": body.email, "moved_to_archive": moved}
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


class DeprovisionPaxtonRequest(PydanticBase):
    paxton_id: int

@router.post("/api/staff/deprovision-paxton")
async def deprovision_paxton(
    body: DeprovisionPaxtonRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Remove all access levels from a Paxton cardholder."""
    from app.integrations.paxton.adapter import PaxtonAdapter

    paxton = PaxtonAdapter(db)

    try:
        pax_user = await paxton.get_user(body.paxton_id)
        if not pax_user:
            raise HTTPException(status_code=404, detail=f"Paxton user not found: {body.paxton_id}")

        current_levels = pax_user.get("access_levels", [])
        display_name = pax_user.get("display_name", str(body.paxton_id))

        from app.modules.settings.repository import get_setting_value

        # Clear access levels in all cases. Move to the deprovision
        # department only if one is configured — no hardcoded district
        # fallback (previous versions had `or "34"` which was
        # the district-specific and would misfile users in other
        # districts' Paxton instances).
        dept_setting = (await get_setting_value(db, "paxton", "deprovision_dept_id") or "").strip()
        dept_id: int | None = None
        if dept_setting:
            try:
                dept_id = int(dept_setting)
            except (ValueError, TypeError):
                logger.warning(
                    f"paxton.deprovision_dept_id is set but not numeric: {dept_setting!r} "
                    f"— skipping department move"
                )

        # Step 1: clear access levels via PUT /users/{id}. The user
        # body does NOT carry department membership — that's a
        # separate endpoint — so this PUT only handles access levels.
        raw_user = await paxton._api_call("GET", f"users/{body.paxton_id}")
        raw_user["doorAccessPermissionSet"] = {"accessLevels": [], "individualPermissions": []}
        try:
            await paxton._api_call("PUT", f"users/{body.paxton_id}", raw_user)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Paxton deprovision failed: {str(e)[:100]}")

        # Step 2: move the user to the deprovision department via the
        # dedicated PUT /users/{id}/departments endpoint. Non-fatal —
        # the access clear already happened, so even if the move fails
        # the user is locked out regardless.
        dept_move_ok = False
        if dept_id is not None:
            mv = await paxton.move_to_department(body.paxton_id, dept_id)
            dept_move_ok = mv.success
            if not mv.success:
                logger.warning(
                    f"Paxton user {body.paxton_id} access cleared but "
                    f"department move to {dept_id} failed: {mv.error}"
                )
        else:
            logger.warning(
                f"Paxton user {body.paxton_id} had access cleared but no "
                f"deprovision_dept_id is configured in Settings — user "
                f"remains in their original department"
            )

        await log_action(
            db, actor=user.email, action="staff.paxton.deprovision",
            module="staff", target=f"{display_name} (Paxton ID {body.paxton_id})",
            details=(
                f"Cleared access + moved to dept {dept_id}" if dept_move_ok
                else f"Cleared access (dept move to {dept_id} failed)" if dept_id is not None
                else "Cleared access (no deprovision_dept_id configured)"
            ) + f" (was: {len(current_levels)} levels)",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {
            "status": "ok",
            "paxton_id": body.paxton_id,
            "previous_levels": len(current_levels),
            "moved_to_inactive": dept_move_ok,
            "deprovision_dept_id": dept_id,
        }
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


# ── API: Staff Queue (HR-driven onboard/deprovision) ─────────────────────

def _queue_row_to_dict(r) -> dict:
    """
    Convert a staff_queue row (plus joined staff_directory.last_login)
    to a serializable dict.

    Uses SQLAlchemy's Row._mapping rather than positional indexing so
    the SELECT clause and this function don't need to stay in lockstep —
    adding a column to _QUEUE_SELECT automatically surfaces it here via
    its alias/name.
    """
    # `r` is a SQLAlchemy Row; _mapping gives a dict-like accessor keyed
    # by column name. For un-aliased SELECT ... FROM staff_queue q, the
    # column names come through without the "q." prefix.
    m = r._mapping if hasattr(r, "_mapping") else dict(zip(r.keys(), r) if hasattr(r, "keys") else {})

    def get(name, default=None):
        try:
            return m[name] if name in m else default
        except Exception:
            return default

    def iso(v):
        return v.isoformat() if v else None

    def load_json(v, fallback):
        if not v:
            return fallback
        try:
            return json.loads(v)
        except Exception:
            return v

    return {
        "id": get("id"),
        "action": get("action"),
        "first_name": get("first_name"),
        "last_name": get("last_name"),
        "email": get("email"),
        "building": get("building"),
        "role_type": get("role_type"),
        "title": get("title"),
        "source": get("source"),
        "status": get("status"),
        "details": get("details"),
        "created_at": iso(get("created_at")),
        "confirmed_by": get("confirmed_by"),
        "confirmed_at": iso(get("confirmed_at")),
        "completed_at": iso(get("completed_at")),
        "error": get("error"),
        "photo_path": get("photo_path"),
        "preferred_name": get("preferred_name"),
        "room": get("room"),
        "classification": get("classification"),
        "position": get("position"),
        "school": get("school"),
        "source_detail": load_json(get("source_detail"), None),
        "systems": load_json(
            get("systems"),
            # AD off by default — most district AD provisions were
            # failing (missing profile ad_ou, unicodePwd on non-LDAPS,
            # etc.) and blocking the rest of the pipeline. Operator
            # can opt in per-row via the AD checkbox on the queue-edit
            # form when the account genuinely needs a network login.
            {"google": True, "ad": False, "paxton": True, "phone": False},
        ),
        "has_password": bool(get("password_hash")),
        "expected_email": get("expected_email"),
        "extension": get("extension"),
        "submitted_by": get("submitted_by"),
        "reviewed_by": get("reviewed_by"),
        "badge_print_status": get("badge_print_status"),
        "badge_print_error": get("badge_print_error"),
        "badge_printed_at": iso(get("badge_printed_at")),
        # Joined from staff_directory so IT can see how stale an account
        # is before deprovisioning it.
        "last_login": get("last_login"),
    }


_QUEUE_SELECT = """
    SELECT q.id, q.action, q.first_name, q.last_name, q.email, q.building,
           q.role_type, q.title, q.source, q.status, q.details, q.created_at,
           q.confirmed_by, q.confirmed_at, q.completed_at, q.error,
           q.photo_path, q.preferred_name, q.room, q.classification,
           q.position, q.school, q.source_detail, q.systems,
           q.password_hash, q.expected_email, q.extension,
           q.submitted_by, q.reviewed_by,
           q.badge_print_status, q.badge_print_error, q.badge_printed_at,
           sd.last_login
    FROM staff_queue q
    LEFT JOIN staff_directory sd
        ON LOWER(sd.email) = LOWER(COALESCE(NULLIF(q.expected_email, ''), q.email))
"""


@router.post("/api/staff/queue")
async def create_queue_entry(
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually create a provisioning queue entry.

    Used by the New Hire form on the Staff page — reception or IT
    enters a staff member directly into the queue without waiting
    for HR sync or room roster diff.

    Body: { first_name, last_name, building, role_type, title, room,
            preferred_name, expected_email, extension, classification,
            position, notes }
    """
    from sqlalchemy import text
    from app.modules.settings.repository import get_setting_value

    body = await request.json()
    first = (body.get("first_name") or "").strip()
    last = (body.get("last_name") or "").strip()
    if not first or not last:
        raise HTTPException(status_code=400, detail="first_name and last_name are required")

    building = (body.get("building") or "").strip().upper() or None
    role_type = (body.get("role_type") or "").strip() or None
    title = (body.get("title") or "").strip() or None
    room = (body.get("room") or body.get("room_number") or "").strip() or None
    preferred = (body.get("preferred_name") or "").strip() or None
    classification = (body.get("classification") or "").strip() or None
    position = (body.get("position") or title or "").strip() or None
    extension = (body.get("extension") or "").strip() or None

    # Auto-compute expected email if not provided
    expected_email = (body.get("expected_email") or body.get("email") or "").strip().lower()
    if not expected_email:
        domain = await get_setting_value(db, "google", "domain") or ""
        if domain:
            from app.modules.staff.service import build_staff_email
            email_template = await get_setting_value(db, "ad", "staff_email_template") or "{first}.{last}@{domain}"
            expected_email = build_staff_email(first, last, domain, email_template)

    # Anything that doesn't map to a column goes into source_detail for
    # later reference (start_date, state_id, phone, needs_sis, notes).
    source_detail = {
        k: body.get(k)
        for k in ("start_date", "state_id", "phone", "needs_sis", "notes")
        if body.get(k) not in (None, "")
    }

    row = await db.execute(text("""
        INSERT INTO staff_queue
            (action, first_name, last_name, email, building, role_type, title,
             source, status, preferred_name, room, classification, position,
             school, expected_email, extension, source_detail, submitted_by,
             created_at)
        VALUES
            ('provision', :first, :last, :email, :building, :role_type, :title,
             'manual', 'pending_data', :preferred, :room, :classification, :position,
             :school, :expected_email, :extension, :source_detail, :submitted_by,
             :ts)
        RETURNING id
    """).bindparams(
        first=first, last=last, email=expected_email,
        building=building, role_type=role_type, title=title,
        preferred=preferred, room=room,
        classification=classification, position=position,
        school=building, expected_email=expected_email,
        extension=extension,
        source_detail=json.dumps(source_detail) if source_detail else None,
        submitted_by=user.email,
        ts=datetime.now(timezone.utc),
    ))
    new_id = row.scalar()

    await log_action(
        db, actor=user.email, action="staff.queue.create",
        module="staff", target=f"{first} {last} ({building or '-'})",
        details=json.dumps({"role_type": role_type, "expected_email": expected_email}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "id": new_id, "expected_email": expected_email}


@router.get("/api/staff/queue/label-printers")
async def get_label_printer_status(
    user: User = Depends(require_action("staff.queue.view")),
    db: AsyncSession = Depends(get_db),
):
    """
    Reachability check for each building's office label (badge) printer.

    Returns one entry per building configured in ``staff_notifications``.
    Reachability is a TCP connect to port 9100 (Brother QL raw print
    port); no bytes are sent, so this is safe to run repeatedly. Only
    buildings that have a printer host configured are returned.
    """
    from app.modules.settings.repository import get_setting_value

    notif = await _get_notification_config(db)
    school_names_raw = await get_setting_value(db, "branding", "school_names") or "{}"
    try:
        school_names = json.loads(school_names_raw)
    except Exception:
        school_names = {}

    enabled = (await get_setting_value(db, "staff", "badge_printing_enabled") or "").lower() == "true"

    async def _probe(host: str, port: int = 9100, timeout: float = 1.5) -> tuple[bool, str | None]:
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True, None
        except asyncio.TimeoutError:
            return False, "timeout"
        except OSError as e:
            return False, str(e)[:80] or type(e).__name__

    targets = []
    for bldg, cfg in (notif or {}).items():
        host = ((cfg or {}).get("label_printer_host") or "").strip()
        if not host:
            continue
        targets.append((bldg.upper(), host))
    targets.sort(key=lambda t: t[0])

    results = await asyncio.gather(*(_probe(host) for _, host in targets)) if targets else []
    printers = [
        {
            "building": bldg,
            "building_name": school_names.get(bldg) or bldg,
            "host": host,
            "reachable": ok,
            "error": err,
        }
        for (bldg, host), (ok, err) in zip(targets, results)
    ]
    return {"enabled": enabled, "printers": printers}


@router.get("/api/staff/queue")
async def get_staff_queue(
    status: str = "pending_data",
    user: User = Depends(require_action("staff.queue.view")),
    db: AsyncSession = Depends(get_db),
):
    """Get staff onboard/deprovision queue."""
    from sqlalchemy import text
    # Support comma-separated statuses. Columns are prefixed with `q.`
    # because _QUEUE_SELECT now joins staff_directory (sd) for last_login.
    if status and "," in status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        placeholders = ", ".join(f":s{i}" for i in range(len(statuses)))
        params = {f"s{i}": s for i, s in enumerate(statuses)}
        where = f"WHERE q.status IN ({placeholders})"
    elif status:
        params = {"status": status}
        where = "WHERE q.status = :status"
    else:
        params = {}
        where = ""
    result = await db.execute(text(
        f"{_QUEUE_SELECT} {where} ORDER BY q.created_at DESC LIMIT 200"
    ).bindparams(**params))
    return {"entries": [_queue_row_to_dict(r) for r in result.all()]}


@router.put("/api/staff/queue/{item_id}")
async def update_queue_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """Inline edit any field on a queue entry. Reception can do this."""
    from sqlalchemy import text
    body = await request.json()

    # Validate item exists and is editable
    row = (await db.execute(text(
        "SELECT id, status FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if row[1] in ("complete", "provisioning"):
        raise HTTPException(status_code=409, detail=f"Cannot edit item in '{row[1]}' status")

    # Whitelist editable fields
    allowed = {
        "first_name", "last_name", "preferred_name", "email", "expected_email",
        "building", "room", "role_type", "title", "position", "classification",
        "school", "extension", "systems",
    }
    updates = {}
    for k, v in body.items():
        if k in allowed:
            if k == "systems" and isinstance(v, dict):
                updates[k] = json.dumps(v)
            else:
                updates[k] = v

    if not updates:
        return {"status": "ok", "updated": []}

    updates["submitted_by"] = user.email
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = item_id
    await db.execute(text(
        f"UPDATE staff_queue SET {set_clause} WHERE id = :id"
    ).bindparams(**updates))

    await log_action(
        db, actor=user.email, action="staff.queue.edit",
        module="staff", target=f"queue#{item_id}",
        details=json.dumps({k: v for k, v in body.items() if k in allowed}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "updated": list(body.keys())}


@router.post("/api/staff/queue/{item_id}/photo")
async def upload_queue_photo(
    item_id: int,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """Upload a photo for a queue entry."""
    from sqlalchemy import text

    row = (await db.execute(text(
        "SELECT id, status FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    # Validate file
    if file.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, or WebP images are accepted")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Photo must be under 5MB")

    # Run through the ID-card photo processor: EXIF-orient (fixes
    # phone-rotated-sideways case), face detection + smart crop, resize
    # to CR80 photo box aspect. Always saves a JPEG regardless of input
    # type since the processor normalizes to RGB JPEG. If processing
    # fails (rare — corrupt image), we still save the raw upload so the
    # operator can inspect it, but leave id_card_status alone.
    from app.modules.staff.photo_processor import process_photo
    result = process_photo(content, prefer_face_detection=True)

    photo_dir = os.path.join("data", "photos")
    os.makedirs(photo_dir, exist_ok=True)
    filename = f"queue_{item_id}.jpg"
    filepath = os.path.join(photo_dir, filename)

    if result.ok:
        with open(filepath, "wb") as f:
            f.write(result.bytes_jpeg)
        # Loose auto-advance gate — orient + resolution pass = ready.
        # Face-not-found is a warning, not a blocker; operator eyeballs
        # the preview on the /staff/id-cards page before printing.
        new_status = "ready"
    else:
        # Save raw so the operator can see what came in
        with open(filepath, "wb") as f:
            f.write(content)
        new_status = "pending_photo"
        logger.warning(f"queue#{item_id} photo processor failed: {result.reason}")

    await db.execute(text("""
        UPDATE staff_queue
        SET photo_path = :path,
            id_card_status = COALESCE(id_card_status, 'pending_photo'),
            id_card_updated_at = NOW()
        WHERE id = :id
    """).bindparams(path=filepath, id=item_id))
    # Only advance to `ready` when the processor succeeded AND the row
    # isn't already `printed` or `skipped` (don't reopen closed entries).
    if result.ok:
        await db.execute(text("""
            UPDATE staff_queue
            SET id_card_status = 'ready'
            WHERE id = :id
              AND id_card_status IN ('pending_photo', 'ready')
        """).bindparams(id=item_id))
    await db.commit()

    return {
        "status": "ok",
        "photo_path": filepath,
        "processed": result.ok,
        "face_found": result.face_found,
        "exif_rotated": result.exif_rotated,
        "warnings": result.warnings,
        "reason": result.reason if not result.ok else None,
        "id_card_status": new_status,
    }


@router.get("/api/staff/queue/{item_id}/photo")
async def get_queue_photo(
    item_id: int,
    user: User = Depends(require_action("staff.queue.view")),
    db: AsyncSession = Depends(get_db),
):
    """Serve a queue entry's photo."""
    from sqlalchemy import text
    row = (await db.execute(text(
        "SELECT photo_path FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="No photo")
    if not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="Photo file missing")
    return FileResponse(row[0])


@router.get("/api/staff/id-card/{email}")
async def get_id_card(
    email: str,
    format: str = Query("pdf", regex="^(pdf|png)$"),
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Render a Paxton-template ID card for one staff member.

    Data sources:
      - staff_directory: first/last name, department, building
      - Paxton user cache: paxton_id (photo file lookup) + custom fields
        (title lives in custom field 10 per the district config)
    """
    from sqlalchemy import text
    from fastapi.responses import Response
    from app.modules.staff.paxton_card import render_card, render_card_pdf, build_ctx_for_staff

    # Pull from all three source-of-truth caches. Title + department
    # get fallback chains because a staff member might have a Paxton
    # record without custom_field_10 filled in, or none at all (ESC
    # staff, contractors) — in those cases we still want a printable
    # card using whatever data the other systems hold.
    row = (await db.execute(text("""
        SELECT sd.first_name, sd.last_name, sd.department AS sd_dept,
               sd.title AS sd_title, sd.building,
               pu.paxton_id, pu.department AS paxton_department,
               ad.title AS ad_title, ad.department AS ad_department
        FROM staff_directory sd
        LEFT JOIN paxton_user_cache pu ON lower(pu.email) = lower(sd.email)
        LEFT JOIN ad_user_cache ad ON lower(ad.email) = lower(sd.email)
        WHERE lower(sd.email) = lower(:email)
    """).bindparams(email=email))).mappings().first()

    # If staff_directory hasn't caught up yet (the daily sync runs
    # after provisioning), synthesize a row from the most-recent
    # staff_queue entry with this email. This is exactly the scenario
    # when an operator hits Print PDF from /staff/id-cards on a
    # freshly-provisioned account.
    if not row:
        # staff_queue has title + position (either can hold the job title
        # depending on how the row was created) but no department column
        # — leave sd_dept null and let the Paxton/AD fallback fill it in.
        q_row = (await db.execute(text("""
            SELECT sq.first_name, sq.last_name,
                   NULL::text AS sd_dept,
                   COALESCE(NULLIF(sq.title, ''), sq.position) AS sd_title,
                   sq.building,
                   pu.paxton_id, pu.department AS paxton_department,
                   ad.title AS ad_title, ad.department AS ad_department
            FROM staff_queue sq
            LEFT JOIN paxton_user_cache pu
              ON lower(pu.email) = lower(:email)
            LEFT JOIN ad_user_cache ad
              ON lower(ad.email) = lower(:email)
            WHERE lower(sq.expected_email) = lower(:email)
               OR lower(sq.email) = lower(:email)
            ORDER BY sq.id DESC
            LIMIT 1
        """).bindparams(email=email))).mappings().first()
        row = q_row

    if not row:
        raise HTTPException(status_code=404, detail="Staff member not found")

    # Fetch Paxton custom fields live (title lives in field 10 per district
    # config). Empty dict if unreachable so the AD/staff_directory
    # fallbacks below can still supply a title.
    custom_fields: dict[int, str] = {}
    if row["paxton_id"]:
        try:
            from app.integrations.paxton.adapter import PaxtonAdapter
            paxton = PaxtonAdapter(db)
            raw = await paxton._api_call("GET", f"users/{row['paxton_id']}")
            for cf in (raw or {}).get("customFields", []):
                cid = cf.get("id")
                if cid:
                    custom_fields[int(cid)] = cf.get("value") or ""
        except Exception as e:
            logger.warning(f"Card render: could not fetch Paxton fields: {e}")

    # Title fallback: Paxton field 10 → AD title → staff_directory title.
    # If all three are empty the card renders with no title (correct —
    # nothing to say). Feed the resolved value into custom_fields[10]
    # so the template's Field10_50 binding picks it up.
    if not custom_fields.get(10):
        fallback_title = (row["ad_title"] or row["sd_title"] or "").strip()
        if fallback_title:
            custom_fields[10] = fallback_title

    # Photo: active staff_photos row → most recent processed queue
    # photo. Both files went through the same photo processor (EXIF
    # orient + face crop + aspect fit). Username is derived from the
    # email local-part per the district username convention — matches
    # whatever upload_profile_photo wrote.
    photo_path = None
    username_from_email = email.split("@")[0] if email else ""
    if username_from_email:
        p_row = (await db.execute(text("""
            SELECT filename FROM staff_photos
            WHERE lower(staff_username) = lower(:u) AND is_active
            LIMIT 1
        """).bindparams(u=username_from_email))).first()
        if p_row:
            cand = os.path.join(MEDIA_DIR, p_row[0])
            if os.path.exists(cand):
                photo_path = cand
    if not photo_path:
        q_row = (await db.execute(text("""
            SELECT photo_path FROM staff_queue
            WHERE lower(expected_email) = lower(:email)
               OR lower(email) = lower(:email)
            ORDER BY id DESC LIMIT 1
        """).bindparams(email=email))).first()
        if q_row and q_row[0] and os.path.exists(q_row[0]):
            photo_path = q_row[0]

    # Department fallback: staff_directory → Paxton → AD.
    dept = (row["sd_dept"] or row["paxton_department"]
            or row["ad_department"] or "")

    staff_ctx = {
        "first_name": row["first_name"] or "",
        "last_name": row["last_name"] or "",
        "department": dept,
        "building_display": row["building"] or "",
        "photo_path": photo_path,
        "custom_fields": custom_fields,
    }

    if format == "png":
        import io as _io
        img = render_card(staff_ctx)
        buf = _io.BytesIO()
        # Embed 300 DPI so browsers + print dialogs size the page to a
        # single CR80 card (else the default 96 DPI stretches it to
        # ~10.5"x6.6" and splits across pages when printed).
        img.save(buf, format="PNG", dpi=(300, 300))
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={"Content-Disposition": f'inline; filename="{email}-idcard.png"'},
        )
    pdf = render_card_pdf(staff_ctx)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{email}-idcard.pdf"'},
    )


@router.post("/api/staff/queue/{item_id}/ready")
async def mark_queue_ready(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """Reception marks entry as ready for IT provisioning."""
    from sqlalchemy import text
    row = (await db.execute(text(
        "SELECT id, status FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")
    # `pending_review` is the landing status for /onboard/{token}
    # self-service submissions. Admins should be able to promote them
    # to `ready` after eyeballing the entry — same transition semantic
    # as pending → ready.
    if row[1] not in ("pending", "pending_data", "pending_review"):
        raise HTTPException(status_code=409, detail=f"Cannot mark as ready from '{row[1]}' status")

    await db.execute(text(
        "UPDATE staff_queue SET status = 'ready', submitted_by = :by WHERE id = :id"
    ).bindparams(by=user.email, id=item_id))

    await log_action(
        db, actor=user.email, action="staff.queue.ready",
        module="staff", target=f"queue#{item_id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/staff/queue/{item_id}/generate-password")
async def generate_queue_password(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """Generate (or regenerate) a temp password for a queue entry."""
    from sqlalchemy import text
    import secrets
    import string
    import hashlib

    row = (await db.execute(text(
        "SELECT id, status FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    # Generate 12-char password with upper+lower+digit+special
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        password = ''.join(secrets.choice(alphabet) for _ in range(12))
        has_upper = any(c.isupper() for c in password)
        has_lower = any(c.islower() for c in password)
        has_digit = any(c.isdigit() for c in password)
        has_special = any(c in "!@#$%^&*" for c in password)
        if has_upper and has_lower and has_digit and has_special:
            break

    # Store hashed — we return plain text once, then it's gone
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    await db.execute(text(
        "UPDATE staff_queue SET password_hash = :ph WHERE id = :id"
    ).bindparams(ph=pwd_hash, id=item_id))
    await db.commit()

    return {"status": "ok", "password": password}


@router.post("/api/staff/queue/{item_id}/provision")
async def provision_queue_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """IT provisions a queue entry — creates accounts across all checked systems."""
    from sqlalchemy import text
    from app.modules.settings.repository import get_setting_value
    from app.modules.staff.provisioning_profiles import get_profile

    # Load queue item
    row = (await db.execute(text(
        f"{_QUEUE_SELECT} WHERE q.id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    item = _queue_row_to_dict(row)
    # 'partial' is allowed so IT can retry a provision that had some
    # systems succeed and others fail. Already-successful steps will
    # succeed again as idempotent no-ops in most cases (Google
    # add_to_group already handles "Member already exists"), or fail
    # loudly a second time so the root cause is unambiguous.
    # `pending_review` accepted so a trusting admin can one-click
    # provision a self-service onboarding entry without first pressing
    # "Mark Ready" — same idempotency + retry story as `pending`.
    if item["status"] not in ("ready", "pending", "pending_data", "partial", "pending_review"):
        raise HTTPException(status_code=409, detail=f"Cannot provision item in '{item['status']}' status")

    # Mark provisioning
    await db.execute(text(
        "UPDATE staff_queue SET status = 'provisioning', reviewed_by = :by WHERE id = :id"
    ).bindparams(by=user.email, id=item_id))
    await db.flush()

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    password = body.get("password", "")

    first_name = item["first_name"]
    last_name = item["last_name"]
    preferred = item.get("preferred_name") or first_name
    email = item.get("expected_email") or item.get("email") or ""
    building = item.get("building") or "*"
    role_type = item.get("role_type") or "*"
    title = item.get("title") or item.get("position") or ""
    ext = item.get("extension") or ""

    # Systems resolution order:
    #   1. Explicit override in the request body — lets the UI's
    #      provision-confirmation modal pass the operator's per-click
    #      choices without needing a separate PUT to the queue row.
    #   2. Whatever's stored on the queue row.
    #   3. Hardcoded fallback (AD off by default — see _queue_row_to_dict).
    if isinstance(body.get("systems"), dict):
        systems = body["systems"]
    else:
        systems = item.get("systems") or {}
        if isinstance(systems, str):
            try:
                systems = json.loads(systems)
            except Exception:
                systems = {"google": True, "ad": False, "paxton": True, "phone": False}

    # Build email if not set
    if not email:
        domain = await get_setting_value(db, "google", "domain") or ""
        if domain:
            from app.modules.staff.service import build_staff_email
            email = build_staff_email(first_name, last_name, domain)

    # Get provisioning profile
    profile = await get_profile(db, building, role_type)

    errors = []
    step_results = {}

    # ── Google ──
    # Refuse to create at root (`/`) — accounts landing there are
    # invisible to the staff-directory sync (which only walks the four
    # Users-* OUs), so a "success" would orphan the user. Better to
    # fail loud and force the operator to fix the provisioning profile.
    google_ou = (profile.get("google_ou") or "").strip()
    if systems.get("google") and not google_ou:
        errors.append(
            f"Google: no google_ou configured for profile "
            f"{building}/{role_type} — fix the provisioning profile and retry"
        )
    elif systems.get("google"):
        try:
            from app.integrations.google.adapter import GoogleWorkspaceAdapter
            google = GoogleWorkspaceAdapter(db)
            g_result = await google.create_account(
                email=email, first_name=first_name, last_name=last_name,
                org_unit=google_ou, temp_password=password,
            )
            if not g_result.success:
                errors.append(f"Google: {g_result.error}")
            else:
                step_results["google"] = "created"
                groups_added = []
                groups_failed = []
                # Provisioning profiles historically stored group shortnames
                # ("the district-District") instead of full addresses. The Google
                # Admin API's groupKey expects a full email; passing a
                # shortname returns 404/403. Auto-append the user's domain
                # and lowercase the local part so both formats work.
                email_domain = email.split("@", 1)[1] if "@" in email else ""
                for group in profile.get("google_groups", []):
                    if "@" not in group and email_domain:
                        group_full = f"{group.strip().lower()}@{email_domain}"
                    else:
                        group_full = group
                    try:
                        gr_result = await google.add_to_group(email, group_full)
                        # add_to_group returns a dataclass, it doesn't raise
                        if getattr(gr_result, "added", False):
                            groups_added.append(group_full)
                        else:
                            err = getattr(gr_result, "error", "unknown")
                            groups_failed.append(f"{group_full} ({err})")
                            errors.append(f"Google group {group_full}: {str(err)[:80]}")
                    except Exception as ge:
                        groups_failed.append(f"{group_full} ({ge})")
                        errors.append(f"Google group {group_full}: {str(ge)[:80]}")
                if groups_added:
                    step_results["google_groups_added"] = groups_added
                if groups_failed:
                    step_results["google_groups_failed"] = groups_failed
                # Add alias for preferred name if different
                if preferred.lower() != first_name.lower():
                    try:
                        import re
                        alias_first = re.sub(r"[^a-z]", "", preferred.strip().lower())
                        alias_last = re.sub(r"[^a-z]", "", last_name.strip().lower())
                        domain = email.split("@")[1] if "@" in email else ""
                        if domain and alias_first:
                            alias = f"{alias_first}.{alias_last}@{domain}"
                            await google.add_alias(email, alias)
                    except Exception as ae:
                        logger.warning(f"Could not add preferred name alias: {ae}")
        except Exception as e:
            errors.append(f"Google: {str(e)[:100]}")

    # ── AD ──
    if systems.get("ad"):
        try:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad = ActiveDirectoryAdapter(db)
            ad_ou = profile.get("ad_ou")
            if not ad_ou:
                # Loud skip — same rationale as Google. A silent skip
                # here previously let AD-required staff (teachers who
                # need a network login) get provisioned to Google only.
                errors.append(
                    f"AD: no ad_ou configured for profile "
                    f"{building}/{role_type} — fix the provisioning profile and retry"
                )
            else:
                # Pass only the relative OU path. The adapter appends
                # base_dn on its own. Previously we also appended base_dn
                # here which doubled it and produced NO_OBJECT failures.
                ou_path = ad_ou.strip()
                if not ou_path.upper().startswith("OU="):
                    ou_path = f"OU={ou_path}"
                ad_result = await ad.create_account(
                    username=email.split("@")[0] if email else f"{first_name[0].lower()}{last_name.lower()}",
                    first_name=first_name, last_name=last_name,
                    ou_dn=ou_path,
                    password=password,
                )
                if not ad_result.success:
                    errors.append(f"AD: {ad_result.error}")
                else:
                    step_results["ad"] = "created"
                    ad_groups_added = []
                    ad_groups_failed = []
                    for group in profile.get("ad_groups", []):
                        try:
                            grp_result = await ad.add_to_group(ad_result.username, group)
                            if getattr(grp_result, "success", False):
                                ad_groups_added.append(group)
                            else:
                                err = getattr(grp_result, "error", "unknown")
                                ad_groups_failed.append(f"{group} ({err})")
                                errors.append(f"AD group {group}: {str(err)[:80]}")
                        except Exception as age:
                            ad_groups_failed.append(f"{group} ({age})")
                            errors.append(f"AD group {group}: {str(age)[:80]}")
                    if ad_groups_added:
                        step_results["ad_groups_added"] = ad_groups_added
                    if ad_groups_failed:
                        step_results["ad_groups_failed"] = ad_groups_failed
        except Exception as e:
            errors.append(f"AD: {str(e)[:100]}")

    # ── Paxton ──
    if systems.get("paxton"):
        try:
            from app.integrations.paxton.adapter import PaxtonAdapter
            paxton = PaxtonAdapter(db)
            pax_level = profile.get("paxton_access_level")
            # Department resolution order:
            #   1. Per-profile paxton_department_id (explicit)
            #   2. Global paxton.provision_dept_id setting (district default)
            #   3. Access-level-name → department-name lookup, since this
            #      district (and most Paxton installs we've seen) uses the
            #      same name for both. "EPE-Teachers" access level lands
            #      the cardholder in the "EPE-Teachers" department.
            pax_dept_id = profile.get("paxton_department_id")
            if not pax_dept_id:
                global_dept = await get_setting_value(db, "paxton", "provision_dept_id") or ""
                if global_dept.strip().isdigit():
                    pax_dept_id = int(global_dept.strip())
            if not pax_dept_id and pax_level:
                try:
                    depts = await paxton.list_departments()
                    match = next(
                        (d for d in depts if (d.get("name") or "").lower() == pax_level.lower()),
                        None,
                    )
                    if match:
                        pax_dept_id = match["id"]
                        logger.info(
                            f"Paxton: resolved department {pax_dept_id} "
                            f"({match['name']}) from access level name"
                        )
                except Exception as de:
                    logger.warning(f"Paxton department-from-level lookup failed: {de}")
            if pax_level or pax_dept_id:
                pax_result = await paxton.create_user(
                    first_name=first_name, last_name=last_name,
                    email=email, title=title,
                    access_level_name=pax_level or "",
                    department_id=pax_dept_id,
                )
                # Per-write audit — separate from the aggregate
                # provision audit at the end of this loop so an
                # individual Paxton write failure is traceable on its
                # own. Previously, mid-loop create_user / set_user_image
                # failures only surfaced via step_results / errors on
                # the queue row, with no independent audit row.
                await log_action(
                    db, actor=user.email,
                    action="staff.paxton.create_user"
                          if pax_result.success else "staff.paxton.create_user_failed",
                    module="staff",
                    target=f"{first_name} {last_name} ({email})",
                    details=(
                        f"paxton_id={pax_result.paxton_id or ''} "
                        f"access_level={pax_level or ''} "
                        f"dept_id={pax_dept_id or ''} "
                        + (f"error={pax_result.error}" if not pax_result.success else "")
                    ),
                )
                if not pax_result.success:
                    errors.append(f"Paxton: {pax_result.error}")
                else:
                    step_results["paxton"] = "created"
                    if pax_dept_id:
                        step_results["paxton_department_id"] = pax_dept_id

                    # Upload photo if the queue entry has one. Convert
                    # whatever format we received (JPEG/PNG/WebP) to the
                    # JPEG bytes Paxton expects.
                    photo = item.get("photo_path")
                    paxton_id = pax_result.paxton_id
                    if photo and os.path.exists(photo) and paxton_id:
                        photo_err: str | None = None
                        photo_ok = False
                        try:
                            from PIL import Image
                            from io import BytesIO
                            with Image.open(photo) as pimg:
                                pimg = pimg.convert("RGB")
                                # Constrain to a reasonable badge size so
                                # we don't upload 5 MB camera originals.
                                pimg.thumbnail((640, 640), Image.LANCZOS)
                                buf = BytesIO()
                                pimg.save(buf, format="JPEG", quality=88)
                                jpeg_bytes = buf.getvalue()
                            photo_ok = await paxton.set_user_image(paxton_id, jpeg_bytes)
                            if photo_ok:
                                step_results["paxton_photo"] = "uploaded"
                            else:
                                errors.append("Paxton photo upload returned False")
                        except Exception as pe:
                            logger.warning(f"Paxton photo upload failed: {pe}")
                            photo_err = f"{type(pe).__name__}: {pe}"[:200]
                            errors.append(f"Paxton photo: {str(pe)[:80]}")
                        # Independent audit for the photo upload attempt
                        # regardless of whether it succeeded or raised.
                        await log_action(
                            db, actor=user.email,
                            action="staff.paxton.set_user_image"
                                  if photo_ok else "staff.paxton.set_user_image_failed",
                            module="staff",
                            target=f"paxton:{paxton_id} ({email})",
                            details=(
                                f"photo_path={photo} "
                                + (f"error={photo_err}" if photo_err else "")
                            ),
                        )
                    elif photo and not paxton_id:
                        errors.append("Paxton photo: no paxton_id returned from create_user")
            else:
                # No access level AND no department configured — record
                # as a loud skip so the queue row lands as `partial`, not
                # `complete`. Historically this was a silent info log,
                # which let door-access-required staff (any building admin,
                # teacher, or classified worker) get provisioned to Google
                # only. The operator has to fix the profile before Paxton
                # will run.
                errors.append(
                    f"Paxton: no access_level or department configured for "
                    f"profile {building}/{role_type} — fix the provisioning "
                    f"profile and retry"
                )
        except Exception as e:
            errors.append(f"Paxton: {str(e)[:100]}")

    # ── Phone ──
    if systems.get("phone") and ext:
        try:
            from app.integrations.grandstream.adapter import GrandstreamAdapter
            phone_adapter = GrandstreamAdapter(db)
            ph_result = await phone_adapter.update_user(ext, first_name=preferred, last_name=last_name)
            step_results["phone"] = "updated"
        except Exception as e:
            errors.append(f"Phone: {str(e)[:100]}")

    # Update queue item status
    # Status resolution:
    #   errors, no successes   → failed    (nothing worked)
    #   errors, some successes → partial   (needs manual follow-up)
    #   no errors, successes   → complete  (clean)
    #   no errors, no successes → failed   (e.g. profile had no systems configured)
    if errors and not step_results:
        final_status = "failed"
    elif errors and step_results:
        final_status = "partial"
    elif step_results:
        final_status = "complete"
    else:
        final_status = "failed"
    # AD error messages sometimes embed literal NUL bytes (the LDAP
    # WILL_NOT_PERFORM response ends with \x00). Postgres rejects NUL
    # in UTF-8 text columns, so the whole UPDATE fails and the user
    # sees an opaque 500 with no captured error detail. Sanitize
    # before persisting.
    err_payload = "; ".join(errors) if errors else None
    if err_payload:
        err_payload = err_payload.replace("\x00", "").replace("\r", " ").strip()
        err_payload = err_payload[:1000]  # cap to avoid TEXT overflow
    await db.execute(text(
        "UPDATE staff_queue SET status = :status, completed_at = :at, error = :err, "
        "email = :email, reviewed_by = :by WHERE id = :id"
    ).bindparams(
        status=final_status, at=datetime.now(timezone.utc), id=item_id,
        err=err_payload,
        email=email, by=user.email,
    ))

    # Clear password hash after provisioning
    await db.execute(text(
        "UPDATE staff_queue SET password_hash = NULL WHERE id = :id"
    ).bindparams(id=item_id))

    await log_action(
        db, actor=user.email,
        action=f"staff.queue.provision.{'complete' if not errors else 'partial'}",
        module="staff",
        target=f"{first_name} {last_name} ({email})",
        details=json.dumps({"steps": step_results, "errors": errors}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    # Send notifications
    try:
        from app.workers.notifications import notify_provisioned
        await _send_provision_notification(db, item, email, user.email)
    except Exception as ne:
        logger.warning(f"Provision notification failed: {ne}")

    # Self-service onboarding welcome — best-effort, no-op unless the
    # queue item came in via /onboard/{token} AND the submitter left a
    # personal email. No password in this email; that's on the printed
    # credential label they collect at the front office.
    try:
        await _send_onboarding_welcome_email(db, item, email)
    except Exception as we:
        logger.warning(f"Onboarding welcome email failed: {we}")

    # Refresh the staff directory so the new hire appears in the UI
    # immediately instead of waiting for the next scheduled 12-hour
    # sync. The reconciliation job runs after the directory sync to
    # rebuild the unified view the directory page reads from.
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings
        _settings = get_settings()
        _redis = await create_pool(RedisSettings.from_dsn(_settings.redis_url))
        _window = int(datetime.now(timezone.utc).timestamp() // 30)
        await _redis.enqueue_job("sync_staff_directory", _job_id=f"sync:provision:{item_id}:{_window}")
        await _redis.enqueue_job("run_staff_reconciliation", _job_id=f"reconciliation:provision:{item_id}:{_window}")
    except Exception as se:
        logger.warning(f"Post-provision sync enqueue failed: {se}")

    # Print temp badge + credentials label to the building's Brother QL.
    # Never blocks or fails provisioning — any error is recorded on the
    # queue row so the entry stays visible for a reprint.
    print_result = None
    try:
        from app.modules.staff.badge_print import print_temp_badge
        # Refresh item with the email we just wrote so the label is accurate
        item_for_print = dict(item)
        item_for_print["expected_email"] = email
        item_for_print["email"] = email
        print_result = await print_temp_badge(
            db=db,
            queue_item=item_for_print,
            temp_password=password,
            actor=user.email,
        )
        await db.commit()
    except Exception as pe:
        logger.warning(f"Badge print invocation failed: {pe}")

    # Queue the real PVC ID card for print. Photo may or may not be
    # available yet — if we already have a processed photo on the queue
    # row (self-service photo passed the processor), go straight to
    # `ready`; else park at `pending_photo` and let a later admin upload
    # advance it. Never blocks provisioning.
    try:
        from sqlalchemy import text as _text
        photo_row = (await db.execute(_text(
            "SELECT photo_path, id_card_status FROM staff_queue WHERE id = :id"
        ).bindparams(id=item["id"]))).first()
        already = photo_row and photo_row[1]
        if not already:
            has_photo = photo_row and photo_row[0] and os.path.exists(photo_row[0])
            initial = "ready" if has_photo else "pending_photo"
            await db.execute(_text("""
                UPDATE staff_queue
                SET id_card_status = :s, id_card_updated_at = NOW()
                WHERE id = :id AND id_card_status IS NULL
            """).bindparams(s=initial, id=item["id"]))
            await db.commit()
    except Exception as ie:
        logger.warning(f"ID card queue insert failed: {ie}")

    return {
        "status": final_status,
        "errors": errors,
        "steps": step_results,
        "badge_print": print_result,
    }


async def _run_deprovision_one(
    db: AsyncSession,
    item: dict,
    *,
    actor_email: str,
    ip_address: str | None = None,
) -> dict:
    """
    Execute deprovision flow for a single queue item. Used by both the
    single-item endpoint and the bulk endpoint so the behavior stays
    identical. Updates the queue row status at the end. Returns a dict
    describing the outcome so the caller can aggregate.

    Caller owns the transaction — this function calls db.flush() but
    NOT commit(). Caller must commit() after calling this (or after a
    batch of calls in the bulk case).

    Never raises — any exception is captured in the result dict and
    logged so a bulk run can continue past individual failures.
    """
    from sqlalchemy import text
    from app.modules.settings.repository import get_setting_value

    item_id = item.get("id")
    first_name = item["first_name"]
    last_name = item["last_name"]
    email = item.get("email") or item.get("expected_email") or ""
    room = item.get("room") or ""

    # Mark in-flight
    await db.execute(text(
        "UPDATE staff_queue SET status = 'provisioning', reviewed_by = :by WHERE id = :id"
    ).bindparams(by=actor_email, id=item_id))
    await db.flush()

    systems = item.get("systems") or {}
    if isinstance(systems, str):
        try:
            systems = json.loads(systems)
        except Exception:
            systems = {"google": True, "ad": True, "paxton": True, "phone": False}

    errors: list[str] = []
    step_results: dict = {}

    # ── Google suspend + archive ──
    if systems.get("google") and email:
        try:
            from app.integrations.google.adapter import GoogleWorkspaceAdapter
            google = GoogleWorkspaceAdapter(db)
            archive_ou = await get_setting_value(db, "google", "deprovision_ou") or "/Archived Accounts"
            sus = await google.suspend_account(email)
            if sus.success:
                await google.move_to_ou(email, archive_ou)
                step_results["google"] = "suspended"
            else:
                errors.append(f"Google: {sus.error}")
        except Exception as e:
            errors.append(f"Google: {str(e)[:100]}")

    # ── AD disable ──
    if systems.get("ad"):
        try:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad = ActiveDirectoryAdapter(db)
            username = email.split("@")[0] if email else ""
            if username:
                ad_user = await ad.get_user(username)
                if ad_user and ad_user.get("enabled"):
                    await ad.disable_account(username)
                    step_results["ad"] = "disabled"
                    depro_ou = await get_setting_value(db, "ad", "deprovision_ou")
                    if depro_ou:
                        base_dn = await get_setting_value(db, "ad", "base_dn") or ""
                        target = f"{depro_ou},{base_dn}" if base_dn and not depro_ou.endswith(base_dn) else depro_ou
                        await ad.move_user(username, target)
        except Exception as e:
            errors.append(f"AD: {str(e)[:100]}")

    # ── Paxton disable ──
    # No hardcoded district dept fallback — skip the department move
    # if not configured, still clear access levels.
    if systems.get("paxton"):
        try:
            from app.integrations.paxton.adapter import PaxtonAdapter
            paxton = PaxtonAdapter(db)
            dept_setting = (await get_setting_value(db, "paxton", "deprovision_dept_id") or "").strip()
            dept_id: int | None = None
            if dept_setting:
                try:
                    dept_id = int(dept_setting)
                except (ValueError, TypeError):
                    logger.warning(
                        f"paxton.deprovision_dept_id not numeric: {dept_setting!r}"
                    )
            pax_users = await paxton.get_users()
            name_lower = f"{first_name} {last_name}".lower().strip()
            pax_match = next(
                (u for u in pax_users
                 if u.get("display_name", "").lower().strip() == name_lower
                 or (u.get("email") or "").lower() == (email or "").lower()),
                None,
            )
            if pax_match:
                # Clear access levels (user body PUT).
                raw = await paxton._api_call("GET", f"users/{pax_match['id']}")
                raw["doorAccessPermissionSet"] = {"accessLevels": [], "individualPermissions": []}
                await paxton._api_call("PUT", f"users/{pax_match['id']}", raw)
                # Department membership is a separate endpoint.
                if dept_id is not None:
                    mv = await paxton.move_to_department(pax_match["id"], dept_id)
                    if not mv.success:
                        logger.warning(
                            f"Paxton user {pax_match['id']} access cleared but "
                            f"department move to {dept_id} failed: {mv.error}"
                        )
                step_results["paxton"] = "disabled"
        except Exception as e:
            errors.append(f"Paxton: {str(e)[:100]}")

    # ── Phone — set to room function or "Empty Room" ──
    if systems.get("phone"):
        ext = item.get("extension") or ""
        if ext:
            try:
                from app.integrations.grandstream.adapter import GrandstreamAdapter
                phone_adapter = GrandstreamAdapter(db)
                room_label = "Empty Room"
                if room:
                    try:
                        room_row = (await db.execute(text(
                            "SELECT assignment FROM room_roster_cache WHERE room = :room LIMIT 1"
                        ).bindparams(room=room))).first()
                        if room_row and room_row[0]:
                            room_label = room_row[0]
                    except Exception:
                        pass
                await phone_adapter.update_user(ext, first_name=room_label, last_name="")
                step_results["phone"] = "cleared"
            except Exception as e:
                errors.append(f"Phone: {str(e)[:100]}")

    # Tri-state final status
    if errors and not step_results:
        final_status = "failed"
    elif errors and step_results:
        final_status = "partial"
    else:
        final_status = "complete"

    await db.execute(text(
        "UPDATE staff_queue SET status = :status, completed_at = :at, "
        "error = :err, reviewed_by = :by WHERE id = :id"
    ).bindparams(
        status=final_status, at=datetime.now(timezone.utc), id=item_id,
        err="; ".join(errors) if errors else None, by=actor_email,
    ))

    await log_action(
        db, actor=actor_email,
        action=f"staff.queue.deprovision.{final_status}",
        module="staff",
        target=f"{first_name} {last_name} ({email})",
        details=json.dumps({"steps": step_results, "errors": errors}),
        ip_address=ip_address,
    )

    # Notifications (non-fatal — log and continue)
    try:
        await _send_deprovision_notification(db, item)
    except Exception as ne:
        logger.warning(f"Deprovision notification failed for {email}: {ne}")

    return {
        "id": item_id,
        "status": final_status,
        "name": f"{first_name} {last_name}",
        "email": email,
        "steps": step_results,
        "errors": errors,
    }


@router.post("/api/staff/queue/{item_id}/deprovision")
async def deprovision_queue_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """IT deprovisions a single queue entry across all checked systems."""
    from sqlalchemy import text

    row = (await db.execute(text(
        f"{_QUEUE_SELECT} WHERE q.id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    item = _queue_row_to_dict(row)
    if item["status"] in ("complete", "provisioning"):
        raise HTTPException(status_code=409, detail=f"Cannot deprovision item in '{item['status']}' status")

    result = await _run_deprovision_one(
        db, item,
        actor_email=user.email,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {
        "status": result["status"],
        "errors": result["errors"],
        "steps": result["steps"],
    }


@router.post("/api/staff/queue/bulk-deprovision")
async def bulk_deprovision_queue(
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """
    Run the deprovision flow on every queue entry in the given id list.

    Body: {"ids": [int, ...]}

    Iterates sequentially (no parallelism — avoids rate limits on
    Google/AD/Paxton and makes error attribution unambiguous). Entries
    already in complete/provisioning state are skipped with a note.
    Returns per-item result plus aggregate counts.

    Gated behind staff.provision.execute — this is the destructive,
    external-side-effect-having counterpart to bulk-dismiss.
    """
    from sqlalchemy import text

    body = await request.json()
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    ids = [int(i) for i in ids]

    # Load all targeted rows in one query
    rows = (await db.execute(text(
        f"{_QUEUE_SELECT} WHERE q.id = ANY(:ids)"
    ).bindparams(ids=ids))).all()
    items_by_id = {r._mapping["id"]: _queue_row_to_dict(r) for r in rows}

    aggregate = {
        "requested": len(ids),
        "processed": 0,
        "complete": 0,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
        "missing": 0,
        "results": [],
    }

    ip_address = request.client.host if request.client else None

    for qid in ids:
        item = items_by_id.get(qid)
        if item is None:
            aggregate["missing"] += 1
            aggregate["results"].append({
                "id": qid,
                "status": "missing",
                "error": "Queue item not found",
            })
            continue
        if item.get("action") != "deprovision":
            aggregate["skipped"] += 1
            aggregate["results"].append({
                "id": qid,
                "status": "skipped",
                "error": f"Not a deprovision entry (action={item.get('action')})",
            })
            continue
        if item.get("status") in ("complete", "provisioning"):
            aggregate["skipped"] += 1
            aggregate["results"].append({
                "id": qid,
                "status": "skipped",
                "error": f"Already in '{item.get('status')}' state",
            })
            continue

        try:
            result = await _run_deprovision_one(
                db, item,
                actor_email=user.email,
                ip_address=ip_address,
            )
            aggregate["processed"] += 1
            aggregate[result["status"]] = aggregate.get(result["status"], 0) + 1
            aggregate["results"].append(result)
            # Commit per-row so a single failure doesn't undo the prior
            # successes (external side effects already happened — the
            # DB state must reflect that).
            await db.commit()
        except Exception as e:
            logger.error(f"Bulk deprovision failed on queue#{qid}: {e}")
            aggregate["failed"] += 1
            aggregate["results"].append({
                "id": qid,
                "status": "failed",
                "error": str(e)[:200],
            })
            # Best-effort — move on to the next one
            try:
                await db.rollback()
            except Exception:
                pass

    # One aggregate audit entry so it's easy to find bulk actions later
    try:
        await log_action(
            db, actor=user.email,
            action="staff.queue.bulk_deprovision",
            module="staff",
            target=f"{aggregate['processed']}/{aggregate['requested']} entries",
            details=json.dumps({
                "complete": aggregate["complete"],
                "partial": aggregate["partial"],
                "failed": aggregate["failed"],
                "skipped": aggregate["skipped"],
                "missing": aggregate["missing"],
            }),
            ip_address=ip_address,
        )
        await db.commit()
    except Exception as ae:
        logger.warning(f"Bulk deprovision aggregate audit failed: {ae}")

    return aggregate


@router.post("/api/staff/queue/{item_id}/reprint-badge")
async def reprint_queue_badge(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Reprint the temp badge + credentials label for a queue entry
    whose first print failed or whose data changed after provisioning.

    The temp password is never stored in plaintext on the queue row,
    so a reprint body MAY include a password if the operator still
    has it; otherwise the password line will be blank.
    """
    from sqlalchemy import text
    from app.modules.staff.badge_print import print_temp_badge

    row = (await db.execute(text(
        f"{_QUEUE_SELECT} WHERE q.id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    item = _queue_row_to_dict(row)

    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    password = body.get("password", "")

    result = await print_temp_badge(
        db=db,
        queue_item=item,
        temp_password=password,
        actor=user.email,
    )
    await db.commit()
    return {"status": "ok", "badge_print": result}


@router.post("/api/staff/queue/{item_id}/dismiss-print")
async def dismiss_queue_badge_print(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Dismiss a pending/failed badge print — used when IT has already
    printed the real PVC card or printed by hand. Clears the print
    error flag without affecting the queue entry's provision status.
    """
    from sqlalchemy import text
    row = (await db.execute(text(
        "SELECT id, badge_print_status FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    await db.execute(text(
        """
        UPDATE staff_queue
        SET badge_print_status = 'dismissed',
            badge_print_error = NULL
        WHERE id = :id
        """
    ).bindparams(id=item_id))

    await log_action(
        db, actor=user.email, action="staff.badge.print.dismissed",
        module="staff", target=f"queue#{item_id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/staff/queue/{item_id}/dismiss")
async def dismiss_queue_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a queue item — false positive, no action taken."""
    from sqlalchemy import text
    row = (await db.execute(text(
        "SELECT id, status FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if row[1] in ("complete", "provisioning"):
        raise HTTPException(status_code=409, detail=f"Cannot dismiss item in '{row[1]}' status")

    await db.execute(text(
        "UPDATE staff_queue SET status = 'dismissed', reviewed_by = :by, completed_at = :at WHERE id = :id"
    ).bindparams(by=user.email, at=datetime.now(timezone.utc), id=item_id))

    await log_action(
        db, actor=user.email, action="staff.queue.dismiss",
        module="staff", target=f"queue#{item_id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/staff/queue/{item_id}/mark-resolved")
async def mark_queue_resolved(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Mark a partial/failed queue item as manually resolved.

    The provisioning system reported an error (missing profile config,
    AD LDAPS unavailability, name collision, etc.) and the operator
    fixed it outside the tool. Rather than pretend it never happened,
    we roll the row forward to ``complete`` while preserving the
    original error text under a ``[resolved: ...]`` marker so the
    audit trail still shows what was wrong.

    Body (optional): ``{"note": "created AD account manually"}``
    """
    from sqlalchemy import text
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    note = (body.get("note") or "").strip()

    row = (await db.execute(text(
        "SELECT id, status, error FROM staff_queue WHERE id = :id"
    ).bindparams(id=item_id))).first()
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")
    if row[1] not in ("partial", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot mark-resolved from '{row[1]}' status "
                   f"(only partial/failed rows can be manually resolved)",
        )

    prev_error = row[2] or ""
    marker = f"[resolved by {user.email}"
    if note:
        marker += f": {note}"
    marker += "]"
    new_error = (prev_error + " " + marker).strip() if prev_error else marker

    await db.execute(text(
        "UPDATE staff_queue SET status = 'complete', reviewed_by = :by, "
        "completed_at = :at, error = :err WHERE id = :id"
    ).bindparams(by=user.email, at=datetime.now(timezone.utc), err=new_error[:1000], id=item_id))

    await log_action(
        db, actor=user.email, action="staff.queue.mark_resolved",
        module="staff", target=f"queue#{item_id}",
        details=json.dumps({"previous_status": row[1], "previous_error": prev_error[:400], "note": note}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/staff/queue/bulk-mark-resolved")
async def bulk_mark_resolved(
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Mark multiple partial/failed queue entries as manually resolved.

    Body: ``{"ids": [int, ...], "note": "..."}``. Only rows in
    partial/failed are touched; anything already complete or in a
    pre-provision status is silently skipped.
    """
    from sqlalchemy import text
    body = await request.json()
    ids = body.get("ids") or []
    note = (body.get("note") or "").strip()
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    ids = [int(i) for i in ids]

    marker = f"[bulk-resolved by {user.email}"
    if note:
        marker += f": {note}"
    marker += "]"

    result = await db.execute(text(
        """
        UPDATE staff_queue
        SET status = 'complete',
            completed_at = :at,
            reviewed_by = :by,
            error = LEFT(COALESCE(error, '') || ' ' || :marker, 1000)
        WHERE id = ANY(:ids)
          AND status IN ('partial', 'failed')
        """
    ).bindparams(at=datetime.now(timezone.utc), by=user.email, marker=marker, ids=ids))
    resolved = result.rowcount or 0

    await log_action(
        db, actor=user.email, action="staff.queue.bulk_mark_resolved",
        module="staff",
        target=f"{resolved} entries",
        details=json.dumps({"requested": len(ids), "resolved": resolved, "note": note}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "resolved": resolved, "requested": len(ids)}


@router.post("/api/staff/queue/{item_id}/confirm")
async def confirm_queue_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Legacy confirm endpoint — redirects to provision."""
    return await provision_queue_item(item_id, request, user, db)


@router.post("/api/staff/queue/{item_id}/reject")
async def reject_queue_item(
    item_id: int,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """Reject a queue item — no action taken."""
    return await dismiss_queue_item(item_id, request, user, db)


@router.post("/api/staff/queue/bulk-dismiss")
async def bulk_dismiss_queue(
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Dismiss multiple queue entries in one call. Body: {"ids": [int, ...]}.
    Entries in pending_data/pending/ready/partial are dismissed —
    anything that's already complete/provisioning/dismissed is silently skipped.
    No external system is touched.
    """
    from sqlalchemy import text
    body = await request.json()
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    ids = [int(i) for i in ids]

    result = await db.execute(text(
        """
        UPDATE staff_queue
        SET status = 'dismissed',
            completed_at = :at,
            reviewed_by = :by,
            error = COALESCE(error, '') || ' [bulk-dismissed by ' || :by || ']'
        WHERE id = ANY(:ids)
          AND status IN ('pending_data', 'pending', 'ready', 'partial')
        """
    ).bindparams(at=datetime.now(timezone.utc), by=user.email, ids=ids))
    dismissed = result.rowcount or 0

    await log_action(
        db, actor=user.email, action="staff.queue.bulk_dismiss",
        module="staff",
        target=f"{dismissed} entries",
        details=json.dumps({"requested": len(ids), "dismissed": dismissed}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "dismissed": dismissed, "requested": len(ids)}


@router.post("/api/staff/queue/bulk-mark-ready")
async def bulk_mark_ready(
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Mark multiple queue entries as 'ready' in one call. Body: {"ids": [int, ...]}.
    """
    from sqlalchemy import text
    body = await request.json()
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    ids = [int(i) for i in ids]

    result = await db.execute(text(
        """
        UPDATE staff_queue
        SET status = 'ready',
            submitted_by = :by
        WHERE id = ANY(:ids)
          AND status IN ('pending_data', 'pending')
        """
    ).bindparams(by=user.email, ids=ids))
    updated = result.rowcount or 0

    await log_action(
        db, actor=user.email, action="staff.queue.bulk_mark_ready",
        module="staff",
        target=f"{updated} entries",
        details=json.dumps({"requested": len(ids), "updated": updated}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "updated": updated, "requested": len(ids)}


@router.post("/api/staff/queue/check-hr")
async def trigger_hr_check(
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger HR diff check."""
    from app.workers.hr_sync_job import sync_hr_data
    result = await sync_hr_data({})
    return result


@router.post("/api/staff/override")
async def set_staff_override(
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Set or update a manual override for a Google staff account.

    Body:
        email: str (required)
        kind: "confirmed" | "non_person" (required)
        note: str (optional — short reason for future reference)
        display_name: str (optional — for UI display)

    Behavior:
        - confirmed   → IT vouches this is real staff. match_state will
                        become 'override' on the next sync, removing it
                        from the deprovision queue.
        - non_person  → service / shared / vendor account. The next sync
                        drops it from staff_directory entirely and any
                        pending deprovision queue entry is dismissed.

    Idempotent: re-posting upserts the existing row and clears restored_at.
    """
    from sqlalchemy import text
    from app.modules.staff.models import StaffIgnore

    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    kind = (body.get("kind") or "").strip().lower()
    note = (body.get("note") or "").strip() or None
    display_name = (body.get("display_name") or "").strip() or None

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="email is required")
    if kind not in ("confirmed", "non_person"):
        raise HTTPException(status_code=400, detail="kind must be 'confirmed' or 'non_person'")

    existing = (await db.execute(
        select(StaffIgnore).where(StaffIgnore.username == email)
    )).scalar_one_or_none()

    if existing:
        existing.kind = kind
        existing.reason = note if note is not None else existing.reason
        existing.display_name = display_name or existing.display_name
        existing.ignored_by = user.email
        existing.restored_at = None
        existing.created_at = datetime.now(timezone.utc)
    else:
        db.add(StaffIgnore(
            username=email,
            kind=kind,
            display_name=display_name,
            reason=note,
            ignored_by=user.email,
        ))

    # Flip match_state on the live staff_directory row so the UI reflects
    # the override IMMEDIATELY — no waiting for the next sync (which
    # rebuilds the whole cache and can be 12h away). The scheduled sync
    # will still overwrite this later using the same logic (ignore kind
    # 'confirmed' → 'override', 'non_person' → 'non_person'), so this is
    # just fast-forwarding the visible state.
    new_state = "override" if kind == "confirmed" else "non_person"
    await db.execute(text(
        "UPDATE staff_directory SET match_state = :state "
        "WHERE LOWER(email) = :email"
    ).bindparams(state=new_state, email=email))

    # If marking non_person, also dismiss any active deprovision entry
    # for this email so it disappears from the queue immediately.
    if kind == "non_person":
        await db.execute(text(
            "UPDATE staff_queue SET status='dismissed', "
            "completed_at=:at, reviewed_by=:by, "
            "error=COALESCE(error,'') || ' [marked non_person]' "
            "WHERE LOWER(email) = :email "
            "AND action='deprovision' "
            "AND status IN ('pending_data','pending','ready')"
        ).bindparams(at=datetime.now(timezone.utc), by=user.email, email=email))

    await log_action(
        db, actor=user.email, action=f"staff.override.{kind}",
        module="staff", target=email,
        details=json.dumps({"note": note, "display_name": display_name}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "email": email, "kind": kind, "note": note}


@router.delete("/api/staff/override/{email}")
async def revoke_staff_override(
    email: str,
    request: Request,
    user: User = Depends(require_action("staff.queue.edit")),
    db: AsyncSession = Depends(get_db),
):
    """
    Revoke a manual override (soft delete via restored_at). The next
    sync will re-evaluate the account against HR + roster + nothing
    else.
    """
    from app.modules.staff.models import StaffIgnore

    email = email.strip().lower()
    existing = (await db.execute(
        select(StaffIgnore).where(StaffIgnore.username == email)
    )).scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=404, detail="No override exists for that email")

    existing.restored_at = datetime.now(timezone.utc)
    await log_action(
        db, actor=user.email, action="staff.override.revoke",
        module="staff", target=email,
        details=json.dumps({"prior_kind": existing.kind}),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "ok", "email": email}


@router.get("/api/staff/overrides")
async def list_staff_overrides(
    kind: str | None = None,
    user: User = Depends(require_action("staff.queue.view")),
    db: AsyncSession = Depends(get_db),
):
    """List all active staff overrides. Optional filter by kind."""
    from app.modules.staff.models import StaffIgnore
    q = select(StaffIgnore).where(StaffIgnore.restored_at.is_(None))
    if kind:
        q = q.where(StaffIgnore.kind == kind)
    rows = (await db.execute(q.order_by(StaffIgnore.created_at.desc()))).scalars().all()
    return {
        "overrides": [
            {
                "email": r.username,
                "kind": r.kind,
                "display_name": r.display_name,
                "note": r.reason,
                "set_by": r.ignored_by,
                "set_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


# ── Queue Notification Helpers ───────────────────────────────────────────

async def _get_notification_config(db) -> dict:
    """Load staff_notifications JSON setting."""
    from app.modules.settings.repository import get_setting_value
    raw = await get_setting_value(db, "staff", "staff_notifications")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


async def _resolve_notification_sender(db) -> str:
    """
    Resolve the Gmail 'From' address used for staff notifications.

    Prefers the explicit ``google.sender_email`` setting so a district
    can route all Nexus notifications through a dedicated mailbox
    (``nexus@district.org``). Falls back to the domain admin email
    configured in ``GOOGLE_ADMIN_EMAIL`` — the service account already
    has domain-wide delegation to impersonate that user for Admin SDK
    calls, so the same identity works for Gmail send as long as the
    ``gmail.send`` scope is authorized on the service account.
    """
    from app.modules.settings.repository import get_setting_value
    from app.config import get_settings
    sender = (await get_setting_value(db, "google", "sender_email") or "").strip()
    if sender:
        return sender
    return get_settings().google_admin_email or ""


async def _send_provision_notification(db, item: dict, email: str, provisioned_by: str):
    """Notify principal + reception that a staff member has been provisioned."""
    from app.workers.notifications import _send_email, _html_wrap
    from app.modules.settings.repository import get_setting_value

    config = await _get_notification_config(db)
    building = (item.get("building") or "").upper()
    bldg_config = config.get(building, {})
    recipients = bldg_config.get("principal", []) + bldg_config.get("reception", [])
    if not recipients:
        logger.info(
            f"Provision notification skipped — no recipients configured "
            f"for building {building!r} in staff_notifications"
        )
        return

    sender = await _resolve_notification_sender(db)
    if not sender:
        logger.warning(
            "Provision notification skipped — no sender configured "
            "(set google.sender_email or GOOGLE_ADMIN_EMAIL env var)"
        )
        return

    app_url = await get_setting_value(db, "branding", "app_url") or ""
    district_name = await get_setting_value(db, "branding", "district_name") or ""
    full_name = f"{item['first_name']} {item['last_name']}"
    subject = f"Staff Provisioned: {full_name}"
    html = _html_wrap(
        f"""
    <p>A new staff member has been provisioned at <strong>{building}</strong>:</p>
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Name</td><td>{full_name}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Position</td><td>{item.get('position') or item.get('title') or '-'}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Building</td><td>{building}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Email</td><td style="font-family:monospace">{email}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Room</td><td>{item.get('room') or '-'}</td></tr>
    </table>
    <p>The new staff member will need to change their password on first login.</p>
    """,
        district_name=district_name,
        app_url=app_url,
        link_label="Open Staff Directory",
        link_path="/staff",
    )

    for r in recipients:
        _send_email(sender, r.strip(), subject, html)


async def _send_onboarding_welcome_email(db, item: dict, email: str) -> dict | None:
    """Send the "your account is ready — visit the front office" email
    to a self-service onboarded new hire's PERSONAL email. Only fires
    when the queue item came in via the /onboard/{token} flow AND the
    submitter provided a personal_email at that time.

    Deliberately does NOT include the password — that prints to the
    building's Brother QL and is collected in person along with a
    temporary badge. Best-effort, per-send audit logged."""
    import asyncio as _asyncio
    from app.modules.settings.repository import get_setting_value
    from app.workers.notifications import notify_onboarding_provisioned

    if (item.get("source") or "") != "self_service_onboarding":
        return None
    source_detail = item.get("source_detail") or {}
    if isinstance(source_detail, str):
        try: source_detail = json.loads(source_detail)
        except Exception: source_detail = {}
    personal_email = (source_detail.get("personal_email") or "").strip().lower()
    if not personal_email:
        return None
    if not email:
        # Google account creation failed — no useful sign-in to hand off
        logger.info(
            "Onboarding welcome skipped — no district email for "
            f"queue#{item.get('id')} (google provisioning likely failed)"
        )
        return {"sent": False, "recipient": personal_email,
                "error": "no district email available"}

    sender = await _resolve_notification_sender(db)
    if not sender:
        return {"sent": False, "recipient": personal_email,
                "error": "notification sender not configured"}

    district_name = (await get_setting_value(db, "branding", "district_name") or "").strip()
    building = (item.get("building") or "").upper()

    outcome: dict
    try:
        ok = await _asyncio.to_thread(
            notify_onboarding_provisioned,
            sender, personal_email,
            first_name=item.get("first_name", ""),
            last_name=item.get("last_name", ""),
            google_email=email,
            building=building,
            front_office_location=building or None,
            district_name=district_name,
        )
        outcome = {"sent": bool(ok), "recipient": personal_email,
                   "error": None if ok else "smtp send returned False"}
    except Exception as e:
        outcome = {"sent": False, "recipient": personal_email,
                   "error": str(e)[:200]}
        logger.warning(
            f"onboarding welcome email failed for {personal_email}: {e}"
        )

    try:
        await log_action(
            db, actor="system",
            action="staff.onboarding.welcome_email",
            module="staff",
            target=f"queue#{item.get('id')} → {personal_email}",
            details=json.dumps({
                **outcome,
                "district_email": email,
                "building": building,
            }),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"audit log for welcome email failed: {e}")

    return outcome


async def _send_deprovision_notification(db, item: dict):
    """Notify principal + reception that a staff member has been deprovisioned."""
    from app.workers.notifications import _send_email, _html_wrap
    from app.modules.settings.repository import get_setting_value

    config = await _get_notification_config(db)
    building = (item.get("building") or "").upper()
    bldg_config = config.get(building, {})
    recipients = bldg_config.get("principal", []) + bldg_config.get("reception", [])
    if not recipients:
        logger.info(
            f"Deprovision notification skipped — no recipients configured "
            f"for building {building!r}"
        )
        return

    sender = await _resolve_notification_sender(db)
    if not sender:
        logger.warning(
            "Deprovision notification skipped — no sender configured"
        )
        return

    app_url = await get_setting_value(db, "branding", "app_url") or ""
    district_name = await get_setting_value(db, "branding", "district_name") or ""
    full_name = f"{item['first_name']} {item['last_name']}"
    subject = f"Staff Deprovisioned: {full_name}"
    html = _html_wrap(
        f"""
    <p>A staff member has been deprovisioned at <strong>{building}</strong>:</p>
    <table style="border-collapse:collapse;margin:16px 0;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Name</td><td>{full_name}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Position</td><td>{item.get('position') or item.get('title') or '-'}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;font-weight:bold">Building</td><td>{building}</td></tr>
    </table>
    <p>All system access has been revoked.</p>
    """,
        district_name=district_name,
        app_url=app_url,
        link_label="Open Staff Directory",
        link_path="/staff",
    )

    for r in recipients:
        _send_email(sender, r.strip(), subject, html)


# ── API: Ignore / Restore ────────────────────────────────────────────────

# Deprecated 2026-04-09: the standalone "Ignore" concept was replaced by
# the unified override model. Callers should use:
#   POST   /api/staff/override               (kind=confirmed | non_person)
#   DELETE /api/staff/override/{email}       (revoke)
# The staff_ignores table is still the storage backend — same rows, same
# schema — but the semantics are now driven by the `kind` column instead
# of the endpoint that created them.
#
# These two 410 stubs stay in place so any stale bookmarks or third-party
# callers get a clear redirect message instead of a silent 404.


@router.post("/api/staff/ignore")
async def ignore_staff_deprecated(request: Request, user: User = Depends(require_action("staff.provision.execute"))):
    """Deprecated — use POST /api/staff/override instead."""
    raise HTTPException(
        status_code=410,
        detail=(
            "POST /api/staff/ignore is deprecated. Use POST /api/staff/override "
            "with {\"email\": ..., \"kind\": \"non_person\" | \"confirmed\", \"note\": ...} instead."
        ),
    )


@router.delete("/api/staff/ignore/{username}")
async def restore_staff_deprecated(
    username: str,
    user: User = Depends(require_action("staff.provision.execute")),
):
    """Deprecated — use DELETE /api/staff/override/{email} instead."""
    raise HTTPException(
        status_code=410,
        detail=(
            "DELETE /api/staff/ignore/{username} is deprecated. "
            "Use DELETE /api/staff/override/{email} instead."
        ),
    )


@router.get("/api/staff/ignored")
async def list_ignored(
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """List all currently ignored staff."""
    from app.modules.staff.models import StaffIgnore

    result = await db.execute(
        select(StaffIgnore).where(StaffIgnore.restored_at == None).order_by(StaffIgnore.created_at)  # noqa: E711
    )
    records = result.scalars().all()
    return [
        {
            "username": r.username,
            "display_name": r.display_name,
            "reason": r.reason,
            "ignored_by": r.ignored_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]


# ── API: Move AD ─────────────────────────────────────────────────────────

@router.post("/api/staff/move-ad")
async def move_ad(
    body: MoveADRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Move an AD user to a different OU."""
    from app.integrations.ad.adapter import ActiveDirectoryAdapter

    ad = ActiveDirectoryAdapter(db)
    try:
        result = await ad.move_user(body.username, body.new_ou_dn)
        if not result.success:
            raise HTTPException(status_code=500, detail=f"AD move failed: {result.error}")
        await log_action(
            db, actor=user.email, action="staff.ad.move",
            module="staff", target=body.username,
            details=f"new_ou={body.new_ou_dn}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok", "username": body.username, "new_ou": body.new_ou_dn}
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


# ── API: Update Title ────────────────────────────────────────────────────

@router.post("/api/staff/update-title")
async def update_title(
    body: UpdateTitleRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Update a staff member's title in AD."""
    from app.integrations.ad.adapter import ActiveDirectoryAdapter

    ad = ActiveDirectoryAdapter(db)
    try:
        result = await ad.update_title(body.username, body.title)
        if not result.success:
            raise HTTPException(status_code=500, detail=f"Title update failed: {result.error}")
        await log_action(
            db, actor=user.email, action="staff.ad.update_title",
            module="staff", target=body.username,
            details=f"title={body.title}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok", "username": body.username, "title": body.title}
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


# ── API: Reconciliation ─────────────────────────────────────────────────

@router.get("/api/staff/reconciliation")
async def staff_reconciliation(
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """Compare AD vs Google vs Paxton — find mismatches."""
    from app.modules.staff.reconciliation import run_reconciliation
    try:
        return await run_reconciliation(db)
    except Exception as e:
        logger.error(f"Reconciliation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── API: Fuzzy Matches ──────────────────────────────────────────────────

@router.get("/api/staff/fuzzy-matches")
async def get_fuzzy_matches(
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """Google directory staff with possible Paxton matches via nickname/fuzzy matching."""
    from app.integrations.paxton.adapter import PaxtonAdapter
    from app.modules.staff.models import StaffDirectoryEntry, StaffLink

    paxton = PaxtonAdapter(db)

    # Get confirmed links by email
    link_result = await db.execute(select(StaffLink).where(StaffLink.confirmed == True))
    confirmed_emails = set()
    for lnk in link_result.scalars().all():
        if lnk.google_email:
            confirmed_emails.add(lnk.google_email.lower())
        if lnk.ad_username:
            confirmed_emails.add(lnk.ad_username.lower())

    # Build staff list from Google directory (source of truth)
    dir_result = await db.execute(
        select(StaffDirectoryEntry).where(StaffDirectoryEntry.status == "active")
    )
    staff = []
    for s in dir_result.scalars().all():
        email = (s.email or "").lower()
        username = email.split("@")[0] if email else ""
        staff.append({
            "username": username,
            "email": email,
            "display_name": s.full_name or f"{s.first_name} {s.last_name}".strip(),
            "first_name": s.first_name or "",
            "last_name": s.last_name or "",
        })

    confirmed = {s["username"]: 0 for s in staff if s["email"] in confirmed_emails or s["username"] in confirmed_emails}
    matches = await paxton.fuzzy_match_users(staff, confirmed)

    # Replace ad_username/ad_email with google-sourced fields
    for m in matches:
        m["google_email"] = next((s["email"] for s in staff if s["username"] == m.get("ad_username")), m.get("ad_email", ""))

    return {"count": len(matches), "matches": matches}


# ── API: Confirm Link ───────────────────────────────────────────────────

@router.post("/api/staff/confirm-link")
async def confirm_link(
    body: ConfirmLinkRequest,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Manually confirm a staff-to-Paxton identity link."""
    from app.modules.staff.models import StaffLink
    from app.integrations.paxton.adapter import PaxtonAdapter
    from app.modules.settings.repository import get_setting_value

    email = body.google_email or f"{body.ad_username}@"  # fallback

    try:
        # Check for existing link by username or email
        existing = await db.execute(
            select(StaffLink).where(
                StaffLink.paxton_id == body.paxton_id,
            )
        )
        link = existing.scalar_one_or_none()
        if link:
            link.confirmed = True
            link.confirmed_by = user.email
            link.confirmed_at = datetime.now(timezone.utc)
            link.google_email = body.google_email or link.google_email
            link.ad_username = body.ad_username or link.ad_username
        else:
            db.add(StaffLink(
                ad_username=body.ad_username,
                paxton_id=body.paxton_id,
                google_email=body.google_email,
                match_type="fuzzy",
                confirmed=True,
                confirmed_by=user.email,
                confirmed_at=datetime.now(timezone.utc),
            ))

        # Push email to Paxton custom field
        if body.google_email:
            try:
                paxton = PaxtonAdapter(db)
                email_fid = int(await get_setting_value(db, "paxton", "custom_field_email") or "9")
                raw = await paxton._api_call("GET", f"users/{body.paxton_id}")
                if raw:
                    cfs = raw.get("customFields", [])
                    email_set = False
                    for cf in cfs:
                        if cf["id"] == email_fid:
                            cf["value"] = body.google_email
                            email_set = True
                            break
                    if not email_set:
                        cfs.append({"id": email_fid, "value": body.google_email})
                    raw["customFields"] = cfs
                    await paxton._api_call("PUT", f"users/{body.paxton_id}", raw)
            except Exception as e:
                logger.warning(f"Could not push email to Paxton on confirm: {e}")

        await log_action(
            db, actor=user.email, action="staff.confirm_link",
            module="staff",
            target=f"{body.google_email or body.ad_username} / Paxton {body.paxton_id}",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception:
        await db.rollback()
        raise


# ── API: Sync Operations ────────────────────────────────────────────────
#
# Manual sync endpoints (POST /api/staff/sync-hr-titles,
# /api/staff/sync-paxton-photos, /api/staff/backfill-paxton-emails) were
# removed — the underlying work is folded into the scheduled sync jobs:
#   - HR title push      → ad_sync_job.sync_ad_users (2x daily)
#   - Paxton email push  → paxton_sync_job.sync_paxton_users (2x daily)
#   - Paxton photo sync  → sync_paxton_photos scheduled job (daily)
# All three stay current automatically; no operator action required.


# ── AD Account Enable / Disable ──────────────────────────────────────────

@router.post("/api/staff/ad/disable/{username}")
async def disable_ad_account(
    username: str,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Disable an AD account (blocks login, stays in current OU)."""
    from app.integrations.ad.adapter import ActiveDirectoryAdapter
    ad = ActiveDirectoryAdapter(db)
    result = await ad.disable_account(username)
    if not result.success:
        raise HTTPException(status_code=502, detail=f"Disable failed: {result.error}")
    await log_action(db, actor=user.email, action="staff.ad.disable",
                     module="staff", target=username,
                     ip_address=request.client.host if request.client else None)
    await db.commit()
    return {"ok": True, "username": username, "action": "disabled"}


@router.post("/api/staff/ad/enable/{username}")
async def enable_ad_account(
    username: str,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Enable an AD account."""
    from app.integrations.ad.adapter import ActiveDirectoryAdapter
    ad = ActiveDirectoryAdapter(db)
    result = await ad.enable_account(username)
    if not result.success:
        raise HTTPException(status_code=502, detail=f"Enable failed: {result.error}")
    await log_action(db, actor=user.email, action="staff.ad.enable",
                     module="staff", target=username,
                     ip_address=request.client.host if request.client else None)
    await db.commit()
    return {"ok": True, "username": username, "action": "enabled"}


# ── Paxton Account Enable / Disable ─────────────────────────────────────
#
# Paxton doesn't expose a simple "enabled" flag we can flip. What
# "disable" means in this codebase is the same thing /deprovision-paxton
# does: clear all access levels AND move the cardholder to the
# "deprovision" department. "Enable" is the inverse: recompute their
# access level + department from the provisioning profile that matches
# their building + role_type, and apply.
#
# Prior to 2026-08-12 these endpoints called paxton.enable_user() /
# paxton.disable_user() — methods that never existed on the adapter,
# so every click 500'd silently. The "Reactivate All" button on the
# staff profile catches the error and shows it inline, but the audit
# trail had no clue what was happening.


async def _paxton_provision_state_for_recon(db, paxton, recon) -> tuple[int | None, int | None, str | None]:
    """
    Resolve (access_level_id, department_id, error) for a reactivation
    from a StaffReconciliation row.

    Order of preference for department:
      1. Per-profile paxton_department_id (explicit override)
      2. Global paxton.provision_dept_id setting
      3. Access-level-name → department-name lookup (district
         convention: same name for both, e.g. "PES-Staff" the access
         level lives in the "PES-Staff" department).
    """
    from app.modules.staff.provisioning_profiles import get_profile
    from app.modules.settings.repository import get_setting_value

    building = (recon.building or "").strip().upper()
    role_type = (recon.role_type or "").strip().lower()
    if not building:
        return None, None, "no building set on staff profile"
    if not role_type:
        return None, None, "no role_type set on staff profile — set it on the reconciliation record first"

    profile = await get_profile(db, building, role_type)
    level_name = (profile.get("paxton_access_level") or "").strip()
    dept_id = profile.get("paxton_department_id")

    level_id: int | None = None
    if level_name:
        level_id = await paxton.get_access_level_for_profile(building, role_type)

    if not dept_id:
        global_dept = (await get_setting_value(db, "paxton", "provision_dept_id") or "").strip()
        if global_dept.isdigit():
            dept_id = int(global_dept)

    if not dept_id and level_name:
        try:
            depts = await paxton.list_departments()
            match = next(
                (d for d in depts if (d.get("name") or "").lower() == level_name.lower()),
                None,
            )
            if match:
                dept_id = match["id"]
        except Exception as e:
            logger.warning(f"Paxton department-from-level lookup failed for {building}/{role_type}: {e}")

    if not level_id and not dept_id:
        return None, None, (
            f"provisioning profile {building}/{role_type} has no paxton_access_level "
            f"and no paxton_department_id, and paxton.provision_dept_id is not set — "
            f"nothing to apply"
        )
    return level_id, dept_id, None


@router.post("/api/staff/paxton/disable/{paxton_id}")
async def disable_paxton_account(
    paxton_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """
    Disable a Paxton user: clear all access levels + move to the
    deprovision department. Mirrors /api/staff/deprovision-paxton but
    takes ``paxton_id`` in the URL for parity with the enable endpoint.
    """
    from app.integrations.paxton.adapter import PaxtonAdapter
    from app.modules.settings.repository import get_setting_value

    paxton = PaxtonAdapter(db)
    pax_user = await paxton.get_user(paxton_id)
    if not pax_user:
        raise HTTPException(status_code=404, detail=f"Paxton user not found: {paxton_id}")

    # Clear access levels via the same PUT pattern deprovision uses.
    try:
        raw_user = await paxton._api_call("GET", f"users/{paxton_id}")
        raw_user["doorAccessPermissionSet"] = {"accessLevels": [], "individualPermissions": []}
        await paxton._api_call("PUT", f"users/{paxton_id}", raw_user)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Paxton disable failed: {str(e)[:100]}")

    dept_moved = False
    dept_id: int | None = None
    dept_setting = (await get_setting_value(db, "paxton", "deprovision_dept_id") or "").strip()
    if dept_setting.isdigit():
        dept_id = int(dept_setting)
        mv = await paxton.move_to_department(paxton_id, dept_id)
        dept_moved = mv.success
        if not mv.success:
            logger.warning(
                f"Paxton user {paxton_id} access cleared but department "
                f"move to {dept_id} failed: {mv.error}"
            )

    from sqlalchemy import text
    await db.execute(
        text("UPDATE paxton_user_cache SET enabled = false WHERE paxton_id = :pid")
        .bindparams(pid=paxton_id)
    )
    await log_action(
        db, actor=user.email, action="staff.paxton.disable",
        module="staff",
        target=f"{pax_user.get('display_name') or paxton_id} (Paxton {paxton_id})",
        details=(
            f"Cleared access + moved to dept {dept_id}" if dept_moved
            else f"Cleared access (dept move to {dept_id} failed)" if dept_id
            else "Cleared access (no deprovision_dept_id configured)"
        ),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {
        "ok": True, "paxton_id": paxton_id, "action": "disabled",
        "moved_to_dept": dept_id if dept_moved else None,
    }


@router.post("/api/staff/paxton/enable/{paxton_id}")
async def enable_paxton_account(
    paxton_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """
    Enable a Paxton user: recompute their access level + department
    from the provisioning profile matching their building + role_type
    on the reconciliation record, and apply.

    A 400 is returned (with a clear reason) if we can't resolve either
    an access level or a department — silently succeeding here is
    what caused the pre-2026-08-12 bug where reactivate looked green
    but didn't actually grant any access.
    """
    from sqlalchemy import select, text
    from app.integrations.paxton.adapter import PaxtonAdapter
    from app.modules.staff.models import StaffReconciliation

    paxton = PaxtonAdapter(db)
    pax_user = await paxton.get_user(paxton_id)
    if not pax_user:
        raise HTTPException(status_code=404, detail=f"Paxton user not found: {paxton_id}")

    # Find the linked staff reconciliation row. Try direct paxton_id
    # link first; fall back to email match so a not-yet-linked
    # cardholder can still be reactivated as long as we can identify
    # the person.
    recon = (await db.execute(
        select(StaffReconciliation).where(StaffReconciliation.paxton_id == paxton_id)
    )).scalar_one_or_none()
    if not recon:
        pax_email = (pax_user.get("email") or "").strip().lower()
        if pax_email:
            recon = (await db.execute(
                select(StaffReconciliation).where(func.lower(StaffReconciliation.email) == pax_email)
            )).scalar_one_or_none()
    if not recon:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Paxton user {paxton_id} isn't linked to any staff record. "
                f"Set paxton_id on the reconciliation row first."
            ),
        )

    level_id, dept_id, err = await _paxton_provision_state_for_recon(db, paxton, recon)
    if err:
        raise HTTPException(status_code=400, detail=f"Cannot enable Paxton: {err}")

    applied: list[str] = []
    if level_id:
        r = await paxton.update_user_access(paxton_id, level_id)
        if not r.success:
            raise HTTPException(status_code=502, detail=f"Access level apply failed: {r.error}")
        applied.append(f"access_level={level_id}")
    if dept_id:
        r = await paxton.move_to_department(paxton_id, dept_id)
        if not r.success:
            raise HTTPException(status_code=502, detail=f"Department move failed: {r.error}")
        applied.append(f"dept={dept_id}")

    await db.execute(
        text("UPDATE paxton_user_cache SET enabled = true WHERE paxton_id = :pid")
        .bindparams(pid=paxton_id)
    )
    await log_action(
        db, actor=user.email, action="staff.paxton.enable",
        module="staff",
        target=f"{pax_user.get('display_name') or paxton_id} (Paxton {paxton_id})",
        details=(
            f"Restored via profile {(recon.building or '').upper()}/"
            f"{(recon.role_type or '').lower()}: {', '.join(applied) or 'nothing applied'}"
        ),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {
        "ok": True, "paxton_id": paxton_id, "action": "enabled",
        "access_level_id": level_id, "department_id": dept_id,
    }


# ── Google Account Suspend / Reactivate ──────────────────────────────────

@router.post("/api/staff/google/suspend/{email}")
async def suspend_google_account(
    email: str,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Suspend a Google account (blocks login, keeps in current OU)."""
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    gws = GoogleWorkspaceAdapter(db)
    existing = await gws.get_user(email)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Google account not found: {email}")
    if existing.get("suspended"):
        raise HTTPException(status_code=400, detail="Account is already suspended")

    result = await gws.suspend_account(email)
    if not result.success:
        raise HTTPException(status_code=502, detail=f"Suspend failed: {result.error}")

    await log_action(
        db, actor=user.email, action="staff.google.suspend",
        module="staff", target=f"{existing.get('full_name', email)} ({email})",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    # Trigger reconciliation to update the directory
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings
        settings = get_settings()
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("run_staff_reconciliation", _job_id=f"reconciliation:suspend:{email}")
    except Exception:
        pass

    return {"ok": True, "email": email, "action": "suspended"}


@router.post("/api/staff/google/reactivate/{email}")
async def reactivate_google_account(
    email: str,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Reactivate a suspended Google account (unblocks login, stays in current OU)."""
    from app.integrations.google.adapter import GoogleWorkspaceAdapter

    gws = GoogleWorkspaceAdapter(db)
    existing = await gws.get_user(email)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Google account not found: {email}")
    if not existing.get("suspended"):
        raise HTTPException(status_code=400, detail="Account is not suspended")

    result = await gws.reactivate_account(email)
    if not result.success:
        raise HTTPException(status_code=502, detail=f"Reactivate failed: {result.error}")

    await log_action(
        db, actor=user.email, action="staff.google.reactivate",
        module="staff", target=f"{existing.get('full_name', email)} ({email})",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    # Trigger reconciliation + staff sync to pick up the reactivated account
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from app.config import get_settings
        settings = get_settings()
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("sync_staff_directory", _job_id=f"sync:reactivate:{email}")
    except Exception:
        pass

    return {"ok": True, "email": email, "action": "reactivated"}


@router.post("/api/staff/google/reset-password/{email}")
async def reset_google_password_and_print(
    email: str,
    request: Request,
    print_label: bool = False,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Generate a temp password, set it on the Google account (force
    change at next login), and OPTIONALLY print a credentials label to
    the staff member's building printer.

    The temp password is returned in the response body so the caller
    can toast/display it. Printing is opt-in via `print_label=true` —
    the on-screen path is now the default because admins usually just
    want to hand the password off verbally or paste it into an email.
    The temp password itself is never written to any audit or log
    (per feedback_audit_actor_never_from_header — the actor + recipient
    + printer host go in the audit; the secret does not).
    """
    import json as _json
    from sqlalchemy import text as _text
    from app.integrations.google.adapter import GoogleWorkspaceAdapter
    from app.modules.staff.service import _generate_temp_password
    from app.modules.staff.badge_render import render_credentials_label
    from app.integrations.brother.adapter import (
        print_images, DEFAULT_MODEL, DEFAULT_LABEL,
    )
    from app.modules.settings.repository import get_setting_value

    gws = GoogleWorkspaceAdapter(db)
    existing = await gws.get_user(email)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Google account not found: {email}")
    if existing.get("suspended"):
        raise HTTPException(
            status_code=400,
            detail="Account is suspended — reactivate before resetting the password.",
        )

    # Building for printer resolution — the staff_directory row is the
    # authoritative "which building" (same source badge_print uses). Fall
    # back to the AD-side row if we didn't sync a directory entry yet.
    row = (await db.execute(_text(
        "SELECT building, first_name, last_name FROM staff_directory "
        "WHERE lower(email) = lower(:e) LIMIT 1"
    ).bindparams(e=email))).mappings().first()
    building = ((row and row["building"]) or "").upper().strip()

    # Google name is the "current Google account info" surface Tim
    # asked for — matches what shows on the profile detail panel.
    name = existing.get("name") or {}
    first_name = (name.get("givenName") or (row and row["first_name"]) or "").strip()
    last_name = (name.get("familyName") or (row and row["last_name"]) or "").strip()
    if not first_name or not last_name:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resolve display name for {email} — refusing to print a label without one.",
        )

    # Printer resolution — only when the caller actually asked to
    # print. Skipping this keeps the on-screen path working even when
    # a building has no printer configured yet.
    printer_host = ""
    printer_model = None
    printer_label = None
    if print_label:
        notif_raw = await get_setting_value(db, "staff", "staff_notifications") or "{}"
        try:
            notif = _json.loads(notif_raw)
        except Exception:
            notif = {}
        bldg_cfg = notif.get(building) or {}
        printer_host = (bldg_cfg.get("label_printer_host") or "").strip()
        if not printer_host:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No label printer configured for building {building or '(unknown)'} — "
                    f"set staff.staff_notifications.{building}.label_printer_host."
                ),
            )
        printer_model = (
            await get_setting_value(db, "staff", "badge_printer_model") or DEFAULT_MODEL
        )
        printer_label = (
            await get_setting_value(db, "staff", "badge_label_media") or DEFAULT_LABEL
        )

    district_name = (
        await get_setting_value(db, "branding", "district_name") or ""
    )

    # 1) Set the password on Google. Done before any print attempt so
    #    that if the print fails, we still have a valid credential to
    #    hand off — never leaves the account with a password nobody
    #    knows.
    temp_password = _generate_temp_password()
    reset_result = await gws.set_password(email, temp_password, force_change=True)
    if not reset_result.success:
        raise HTTPException(
            status_code=502, detail=f"Password reset failed: {reset_result.error}"
        )

    # 2) Optionally render + print the credentials label.
    print_error: str | None = None
    print_bytes = 0
    if print_label:
        try:
            creds_img = render_credentials_label(
                first_name=first_name,
                last_name=last_name,
                email=email,
                temp_password=temp_password,
                district_name=district_name,
            )
            pr = await print_images(
                [creds_img], host=printer_host, model=printer_model, label=printer_label,
            )
            if not pr.success:
                print_error = pr.error or "print failed (no error message)"
            else:
                print_bytes = pr.bytes_sent
        except Exception as e:
            print_error = f"{type(e).__name__}: {e}"[:200]

    # 3) Audit — never log the secret itself; log actor, recipient,
    #    printer host (or "on-screen"), print status. Enough to trace
    #    who reset whose password + where it landed without leaving
    #    the password on disk anywhere.
    await log_action(
        db, actor=user.email,
        action="staff.google.password_reset",
        module="staff",
        target=f"{first_name} {last_name} ({email})",
        details=(
            f"building={building} "
            f"delivery={'label:' + printer_host if print_label else 'on-screen'} "
            f"printed={print_label and print_error is None} "
            + (f"print_error={print_error}" if print_error else "")
        ),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return {
        "ok": True,
        "email": email,
        "action": "password_reset",
        "printed": bool(print_label and not print_error),
        "print_requested": bool(print_label),
        "print_host": printer_host,
        "print_error": print_error,
        "print_bytes": print_bytes,
        "temp_password": temp_password,
    }


# ── HR Unmatched (people in HR without Google accounts) ──────────────────

@router.get("/api/staff/hr-unmatched")
async def hr_unmatched(
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
    building: str = "",
):
    """HR staff who don't have a matching Google account (with nickname awareness)."""
    from sqlalchemy import text
    from app.modules.staff.nicknames import get_nickname_variants

    # Load all HR entries
    hr_query = "SELECT name, email, position, school, classification FROM hr_staff_cache WHERE name IS NOT NULL AND name != ''"
    hr_params = {}
    if building:
        hr_query += " AND school ILIKE :bldg"
        hr_params["bldg"] = f"%{building}%"
    hr_query += " ORDER BY name"
    hr_rows = await db.execute(text(hr_query).bindparams(**hr_params) if hr_params else text(hr_query))
    hr_entries = hr_rows.all()

    # Load all Google staff for matching
    google_rows = await db.execute(text("SELECT lower(email), lower(first_name), lower(last_name) FROM staff_directory"))
    google_emails = set()
    google_names = set()  # "first last" exact
    google_by_last = {}   # last → set of firsts
    for email, first, last in google_rows.all():
        if email:
            google_emails.add(email)
        if first and last:
            google_names.add(f"{first} {last}")
            google_by_last.setdefault(last, set()).add(first)

    unmatched = []
    for name, email, position, school, classification in hr_entries:
        hr_email = (email or "").strip().lower()
        # Strip suffixes (Jr., II, III, IV, Sr.) before matching
        import re as _re
        clean_name = _re.sub(r',?\s*(Jr\.?|Sr\.?|II|III|IV|V)\s*$', '', name.strip(), flags=_re.IGNORECASE).strip()
        parts = clean_name.split()
        if len(parts) < 2:
            continue
        hr_first = parts[0].lower()
        hr_last = " ".join(parts[1:]).lower()

        # Check email match
        if hr_email in google_emails:
            continue

        # Check exact name match
        if f"{hr_first} {hr_last}" in google_names:
            continue

        # Check nickname match — does any variant of the HR first name match a Google first name with the same last name?
        google_firsts = google_by_last.get(hr_last, set())
        if google_firsts:
            variants = get_nickname_variants(hr_first)
            if variants & google_firsts:
                continue

        # Check hyphenated last name — try matching on each segment
        if "-" in hr_last:
            matched_hyphen = False
            for segment in hr_last.split("-"):
                segment = segment.strip()
                if not segment:
                    continue
                if f"{hr_first} {segment}" in google_names:
                    matched_hyphen = True
                    break
                seg_firsts = google_by_last.get(segment, set())
                if seg_firsts:
                    variants = get_nickname_variants(hr_first)
                    if variants & seg_firsts:
                        matched_hyphen = True
                        break
            if matched_hyphen:
                continue

        unmatched.append({"name": name, "email": email, "position": position, "school": school, "classification": classification, "status": "missing"})

    # Check if any "missing" staff are actually suspended in Google
    if unmatched:
        try:
            from app.integrations.google.adapter import GoogleWorkspaceAdapter
            gws = GoogleWorkspaceAdapter(db)
            for entry in unmatched:
                hr_email = (entry.get("email") or "").strip()
                if not hr_email:
                    continue
                try:
                    user = await gws.get_user(hr_email)
                    if user and user.get("suspended"):
                        entry["status"] = "suspended"
                        entry["google_email"] = user.get("email", hr_email)
                except Exception:
                    pass
        except Exception:
            pass

    return {"staff": unmatched}


# ── Name Sync ────────────────────────────────────────────────────────────

@router.post("/api/staff/sync-name")
async def sync_name_to_system(
    request: Request,
    user: User = Depends(require_action("staff.settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Push Google name to another system (UCM, Paxton, AD)."""
    body = await request.json()
    system = body.get("system")
    first_name = body.get("first_name", "").strip()
    last_name = body.get("last_name", "").strip()
    target_id = body.get("target_id")  # extension for UCM, paxton_id for Paxton, username for AD

    if not system or not first_name or not last_name or not target_id:
        raise HTTPException(status_code=400, detail="system, first_name, last_name, and target_id required")

    full_name = f"{first_name} {last_name}"
    result = {"system": system, "name": full_name, "success": False}

    if system == "ucm":
        try:
            from app.integrations.grandstream.adapter import ucm_session
            async with ucm_session(db) as ucm:
                await ucm.update_user(target_id, first_name, last_name)
            # Update local cache
            from sqlalchemy import text as _st
            await db.execute(_st(
                "UPDATE phone_extension_cache SET caller_id_name = :name WHERE extension = :ext"
            ).bindparams(name=full_name, ext=target_id))
            result["success"] = True
        except Exception as e:
            result["error"] = str(e)[:200]

    elif system == "paxton":
        try:
            from app.integrations.paxton.adapter import PaxtonAdapter
            paxton = PaxtonAdapter(db)
            await paxton.update_user_name(int(target_id), first_name, last_name)
            # Update local cache
            from sqlalchemy import text as _st
            await db.execute(_st(
                "UPDATE paxton_user_cache SET first_name = :fn, last_name = :ln WHERE paxton_id = :pid"
            ).bindparams(fn=first_name, ln=last_name, pid=int(target_id)))
            result["success"] = True
        except Exception as e:
            result["error"] = str(e)[:200]

    elif system == "ad":
        try:
            from app.integrations.ad.adapter import ActiveDirectoryAdapter
            ad = ActiveDirectoryAdapter(db)
            await ad.rename_user(target_id, first_name, last_name)
            # Update local cache
            from sqlalchemy import text as _st
            await db.execute(_st(
                "UPDATE ad_user_cache SET display_name = :dn WHERE username = :un"
            ).bindparams(dn=full_name, un=target_id))
            result["success"] = True
        except Exception as e:
            result["error"] = str(e)[:200]
    else:
        raise HTTPException(status_code=400, detail=f"Unknown system: {system}")

    if result["success"]:
        await log_action(
            db, actor=user.email, action=f"staff.name_sync.{system}",
            module="staff", target=f"{system}: {target_id} → '{full_name}'",
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        # Trigger reconciliation to update the pre-computed table
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            from app.config import get_settings
            settings = get_settings()
            redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            window = int(datetime.now(timezone.utc).timestamp() // 30)
            await redis.enqueue_job("run_staff_reconciliation", _job_id=f"reconciliation:{window}")
        except Exception as e:
            logger.warning(f"Failed to enqueue reconciliation after name sync: {e}")

    return result


# ── Room Roster ──────────────────────────────────────────────────────────

@router.post("/api/staff/sync-room-roster")
async def sync_room_roster(
    request: Request,
    user: User = Depends(require_action("staff.settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Sync room assignments from principal Google Sheets."""
    from app.integrations.google.room_roster import sync_room_rosters

    result = await sync_room_rosters(db)
    await log_action(
        db, actor=user.email, action="staff.sync_room_roster",
        module="staff", target=f"{result.get('synced', 0)} assignments",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return result


@router.get("/api/staff/room-roster")
async def get_room_roster(
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
    building: str = "",
):
    """Get cached room assignments, optionally filtered by building."""
    from sqlalchemy import text

    query = "SELECT building, room, name, assignment, floor FROM room_roster_cache"
    params = {}
    if building:
        query += " WHERE building = :bldg"
        params["bldg"] = building.upper()
    query += " ORDER BY building, room"

    rows = await db.execute(text(query).bindparams(**params) if params else text(query))
    return {"rooms": [
        {"building": r[0], "room": r[1], "name": r[2], "assignment": r[3], "floor": r[4]}
        for r in rows.all()
    ]}


@router.get("/api/staff/extension-mismatches")
async def get_extension_mismatches(
    user: User = Depends(require_action("staff.view")),
    db: AsyncSession = Depends(get_db),
):
    """Cross-reference room roster against phone extensions to find caller ID mismatches."""
    from app.modules.staff.room_sync import find_extension_mismatches
    mismatches = await find_extension_mismatches(db)
    return {"mismatches": mismatches, "count": len(mismatches)}


@router.post("/api/staff/fix-extension-name")
async def fix_extension_name(
    request: Request,
    user: User = Depends(require_action("staff.settings.manage")),
    db: AsyncSession = Depends(get_db),
):
    """
    Update a phone extension's caller ID name to match the room roster.
    Requires confirmation — shows what will change before applying.
    """
    body = await request.json()
    extension = body.get("extension")
    new_name = body.get("name")

    if not extension or not new_name:
        raise HTTPException(status_code=400, detail="extension and name required")

    # Split name for UCM (first + last)
    parts = new_name.strip().split()
    if len(parts) >= 2:
        first_name = parts[0]
        last_name = " ".join(parts[1:])
    else:
        first_name = new_name.strip()
        last_name = ""

    # Get current name on the extension for audit
    from sqlalchemy import text as _txt
    current = await db.execute(_txt(
        "SELECT caller_id_name FROM phone_extension_cache WHERE extension = :ext"
    ).bindparams(ext=extension))
    current_row = current.first()
    current_name = current_row[0] if current_row else "unknown"

    # Push to UCM
    try:
        from app.integrations.grandstream.adapter import ucm_session
        async with ucm_session(db) as ucm:
            result = await ucm.update_user(extension, first_name, last_name)
            if not result:
                raise HTTPException(status_code=502, detail="UCM update failed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"UCM error: {str(e)[:200]}")

    # Update local cache
    await db.execute(_txt(
        "UPDATE phone_extension_cache SET caller_id_name = :name WHERE extension = :ext"
    ).bindparams(name=new_name.strip(), ext=extension))

    await log_action(
        db, actor=user.email, action="staff.extension.name_update",
        module="staff",
        target=f"ext {extension}: '{current_name}' → '{new_name.strip()}'",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return {"ok": True, "extension": extension, "old_name": current_name, "new_name": new_name.strip()}
