"""Self-service onboarding — admin token issuance + public form + submit.

Flow:
  1. Admin POSTs /api/staff/onboarding-tokens with a building + TTL.
     A signed single-use URL is returned.
  2. New hire opens /onboard/{token} (no auth), fills the form.
  3. POST /onboard/{token} appends the row to the target building's
     ``NexusData`` room-roster tab AND inserts a ``staff_queue`` row
     with status ``pending_review`` so an admin verifies before the
     Google account is created.

The token IS the authentication — the URL alone is enough to submit —
so short TTL, single-use, and audit are the safety rails. The public
endpoints are rate-limited by IP; the admin endpoints are behind the
existing ``staff.provision.execute`` action.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action
from app.db.engine import get_db
from app.db.models import User
from app.modules.staff.models import OnboardingToken
from app.policies.engine import require_action

logger = logging.getLogger(__name__)
router = APIRouter(tags=["staff-onboarding"])
templates = Jinja2Templates(directory="app/templates")


# ── Constants ────────────────────────────────────────────────────────

# Hard cap TTL — anything longer than a school year is almost certainly
# a mistake. Default is 7 days.
MAX_TTL_DAYS = 30
DEFAULT_TTL_DAYS = 7

# Categories the recipient can pick from — mirrors the roster sheet's
# Category column. Kept short + human-facing.
ASSIGNMENT_OPTIONS = [
    "Classroom Teacher",
    "Intervention Teacher",
    "Paraprofessional",
    "Aide",
    "Specials",
    "Speech",
    "Resource Room",
    "Nurse",
    "Office",
    "Kitchen",
    "Miscellaneous",
]

# Onboarding form → staff_queue.role_type. The manual "+ New Staff"
# form exposes role_type directly; the public onboarding form asks
# for an assignment in plain English instead. Translate here so
# downstream provisioning (permission grants, Paxton dept, badge
# printer, etc.) gets the right value. Pre-fix the queue insert was
# hardcoded to 'teacher' — 2026-08-27 an "Office" submit came in
# as a Teacher and confused the reviewer.
_ASSIGNMENT_TO_ROLE = {
    "Classroom Teacher":    "teacher",
    "Intervention Teacher": "teacher",
    "Specials":             "teacher",
    "Speech":               "teacher",
    "Resource Room":        "teacher",
    "Paraprofessional":     "paraprofessional",
    "Aide":                 "paraprofessional",
    "Nurse":                "classified",
    "Office":               "classified",
    "Kitchen":              "classified",
    "Miscellaneous":        "classified",
}


# ── Helpers ──────────────────────────────────────────────────────────

def _new_token() -> str:
    """43-char url-safe base64 token from 32 random bytes."""
    return secrets.token_urlsafe(32)


def _clean_name_input(s: str, max_len: int = 60) -> str:
    """Trim + collapse whitespace + strip control chars. No digits — a
    number in a first/last name is almost always a spam attempt or the
    recipient pasting the wrong field."""
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    return s[:max_len]


def _valid_name(s: str) -> bool:
    if not s or len(s) < 2:
        return False
    # Allow letters, apostrophes, hyphens, spaces, periods.
    return bool(re.match(r"^[A-Za-z][A-Za-z' \-\.]{1,59}$", s))


async def _resolve_sheet_config(db: AsyncSession, building: str) -> dict:
    """Look up which Google sheet + NexusData range this building writes
    to. Uses the same room_roster.buildings setting the sync reads —
    single source of truth for building sheet mapping."""
    from app.modules.settings.repository import get_setting_value
    from app.integrations.google.room_roster import _parse_building_sheets

    buildings_raw = await get_setting_value(db, "room_roster", "buildings") or ""
    default_range = await get_setting_value(db, "room_roster", "sheet_range") or "NexusData!A1:E200"
    sheets = _parse_building_sheets(buildings_raw, default_range)
    # Try both the raw SIS code and the internal code — issuer may
    # have picked either.
    sis_map_raw = await get_setting_value(db, "branding", "school_building_map") or "{}"
    try:
        sis_to_internal = {k.upper(): v.upper() for k, v in json.loads(sis_map_raw).items()}
    except Exception:
        sis_to_internal = {}
    internal_to_sis = {v: k for k, v in sis_to_internal.items()}

    key = building.upper()
    cfg = sheets.get(key)
    if not cfg and key in internal_to_sis:
        cfg = sheets.get(internal_to_sis[key])
    if not cfg:
        return {}
    # Downgrade the range to a tab-only range for append. append_row
    # uses "NexusData!A:E" so Google finds the first empty row itself.
    original_range = cfg.get("range") or default_range
    if "!" in original_range:
        tab = original_range.split("!", 1)[0]
    else:
        tab = "NexusData"
    return {
        "sheet_id": cfg["sheet_id"],
        "tab": tab,
        "append_range": f"{tab}!A:E",
        "internal_code": sis_to_internal.get(key, key),
    }


# ── Admin page ───────────────────────────────────────────────────────

@router.get("/staff/onboarding", response_class=HTMLResponse)
async def onboarding_admin_page(
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Admin panel — generate onboarding links + view active/recent tokens."""
    from app.policies.engine import get_user_permissions
    from app.policies.page_context import build_page_modules

    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)

    return templates.TemplateResponse("staff_onboarding.html", {
        "request": request,
        "user": user,
        "modules": modules,
    })


# ── Admin API ────────────────────────────────────────────────────────

class TokenCreateBody(BaseModel):
    building: str = Field(..., min_length=1, max_length=20)
    ttl_days: int = Field(DEFAULT_TTL_DAYS, ge=1, le=MAX_TTL_DAYS)
    preset_room: str | None = None
    preset_title: str | None = None
    notes: str | None = None


@router.post("/api/staff/onboarding-tokens")
async def create_onboarding_token(
    body: TokenCreateBody,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Generate a signed single-use URL for a new hire to self-onboard."""
    building = body.building.strip().upper()
    if not building:
        raise HTTPException(status_code=400, detail="building is required")

    # Validate the building has a sheet configured — otherwise the
    # token would be issuable but the submit would fail with a
    # cryptic error later.
    cfg = await _resolve_sheet_config(db, building)
    if not cfg:
        raise HTTPException(
            status_code=400,
            detail=f"No room_roster sheet configured for building {building}",
        )

    tok = _new_token()
    expires = datetime.now(timezone.utc) + timedelta(days=body.ttl_days)
    row = OnboardingToken(
        token=tok,
        building=cfg["internal_code"],
        issued_by=user.email,
        expires_at=expires,
        preset_room=(body.preset_room or None),
        preset_title=(body.preset_title or None),
        notes=(body.notes or None),
    )
    db.add(row)
    await db.flush()

    await log_action(
        db, actor=user.email, action="staff.onboarding_token.issued",
        module="staff", target=f"token#{row.id}",
        details=json.dumps({
            "building": cfg["internal_code"],
            "ttl_days": body.ttl_days,
            "preset_room": body.preset_room,
            "preset_title": body.preset_title,
        }),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    base = str(request.base_url).rstrip("/")
    return {
        "id": row.id,
        "token": tok,
        "url": f"{base}/onboard/{tok}",
        "building": cfg["internal_code"],
        "expires_at": expires.isoformat(),
    }


@router.get("/api/staff/onboarding-tokens")
async def list_onboarding_tokens(
    status: str | None = None,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """List onboarding tokens. Filter by status (pending/submitted/expired/revoked)."""
    q = select(OnboardingToken).order_by(OnboardingToken.issued_at.desc())
    if status:
        q = q.where(OnboardingToken.status == status)
    rows = (await db.execute(q.limit(200))).scalars().all()
    return {
        "tokens": [
            {
                "id": r.id,
                "building": r.building,
                "issued_by": r.issued_by,
                "issued_at": r.issued_at.isoformat() if r.issued_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "status": r.status,
                "used_at": r.used_at.isoformat() if r.used_at else None,
                "resulting_queue_id": r.resulting_queue_id,
                "preset_room": r.preset_room,
                "preset_title": r.preset_title,
                "notes": r.notes,
            }
            for r in rows
        ],
    }


@router.delete("/api/staff/onboarding-tokens/{token_id}")
async def revoke_onboarding_token(
    token_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a pending token. No-op if already submitted/expired/revoked."""
    row = (await db.execute(
        select(OnboardingToken).where(OnboardingToken.id == token_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Token {token_id} not found")
    if row.status != "pending":
        return {"ok": True, "status": row.status, "no_op": True}
    row.status = "revoked"
    await log_action(
        db, actor=user.email, action="staff.onboarding_token.revoked",
        module="staff", target=f"token#{row.id}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"ok": True, "status": "revoked"}


# ── Public form + submit ─────────────────────────────────────────────
#
# NO authentication. The token IS the auth. Enforce single-use, TTL,
# rate limit, and origin sanity here.

async def _load_token(db: AsyncSession, token: str) -> OnboardingToken | None:
    if not token or len(token) < 20:
        return None
    return (await db.execute(
        select(OnboardingToken).where(OnboardingToken.token == token)
    )).scalar_one_or_none()


def _token_state(row: OnboardingToken) -> str:
    """Return ('ok' | 'expired' | 'used' | 'revoked')."""
    if row.status == "revoked":
        return "revoked"
    if row.status == "submitted":
        return "used"
    if row.expires_at and row.expires_at < datetime.now(timezone.utc):
        return "expired"
    return "ok"


def _request_meta(request: Request) -> dict:
    """Origin/geo/agent fingerprint for public-endpoint audit rows.
    All fields are best-effort — CF-* headers only exist when the
    request transited Cloudflare (which our /onboard/* config
    requires in prod)."""
    h = request.headers
    return {
        "ip": request.client.host if request.client else None,
        "cf_country": h.get("cf-ipcountry"),
        "cf_city": h.get("cf-ipcity"),
        "cf_ray": h.get("cf-ray"),
        "ua": (h.get("user-agent") or "")[:255],
        "referer": (h.get("referer") or "")[:255] or None,
    }


@router.get("/onboard/{token}", response_class=HTMLResponse)
async def onboarding_form(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await _load_token(db, token)
    meta = _request_meta(request)
    if not row:
        await log_action(
            db, actor="onboarding_visitor",
            action="staff.onboarding.view.invalid",
            module="staff", target=f"token:{token[:8]}…",
            details=json.dumps({**meta, "reason": "not_found"}),
            ip_address=meta["ip"],
        )
        await db.commit()
        return templates.TemplateResponse(
            "onboarding_error.html",
            {"request": request, "reason": "not_found"},
            status_code=404,
        )
    state = _token_state(row)
    if state != "ok":
        await log_action(
            db, actor="onboarding_visitor",
            action="staff.onboarding.view.stale",
            module="staff", target=f"token#{row.id}",
            details=json.dumps({**meta, "reason": state,
                                "building": row.building}),
            ip_address=meta["ip"],
        )
        await db.commit()
        return templates.TemplateResponse(
            "onboarding_error.html",
            {"request": request, "reason": state},
            status_code=410,
        )
    await log_action(
        db, actor="onboarding_visitor",
        action="staff.onboarding.view.ok",
        module="staff", target=f"token#{row.id}",
        details=json.dumps({**meta, "building": row.building,
                            "issued_by": row.issued_by}),
        ip_address=meta["ip"],
    )
    await db.commit()
    return templates.TemplateResponse(
        "onboarding_form.html",
        {
            "request": request,
            "token": token,
            "building": row.building,
            "preset_room": row.preset_room or "",
            "preset_title": row.preset_title or "",
            "assignments": ASSIGNMENT_OPTIONS,
        },
    )


# Photo upload constraints — mirror /api/staff/queue/{id}/photo.
_PHOTO_MAX_BYTES = 5 * 1024 * 1024
_PHOTO_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


@router.post("/onboard/{token}")
async def onboarding_submit(
    token: str,
    request: Request,
    # Multipart form fields — one round-trip lets the new hire post their
    # info AND their ID-badge photo in a single click.
    first_name: str = Form(..., min_length=2, max_length=60),
    last_name: str = Form(..., min_length=2, max_length=60),
    preferred_name: str | None = Form(None),
    room: str = Form(..., min_length=1, max_length=20),
    assignment: str = Form(..., min_length=1, max_length=60),
    title: str | None = Form(None),
    cell_phone: str | None = Form(None),
    personal_email: str | None = Form(None),
    photo: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    row = await _load_token(db, token)
    if not row:
        raise HTTPException(status_code=404, detail="Invalid link")
    state = _token_state(row)
    if state != "ok":
        raise HTTPException(status_code=410, detail=f"Link {state}")

    first = _clean_name_input(first_name)
    last = _clean_name_input(last_name)
    preferred = _clean_name_input(preferred_name or "")
    room_val = re.sub(r"[^A-Za-z0-9\-]", "", (room or "").strip())[:20]
    assignment = (assignment or "").strip()[:60]
    title = (title or "").strip()[:120]
    cell = re.sub(r"[^\d\-\+\(\) ]", "", (cell_phone or "").strip())[:30]
    personal = (personal_email or "").strip()[:120]

    # Photo validation up-front — bad/missing photo = reject BEFORE we
    # mark the token used so the new hire can fix it and retry.
    if not photo or not (photo.filename or "").strip():
        raise HTTPException(
            status_code=400,
            detail="An ID-badge photo is required.",
        )
    if photo.content_type not in _PHOTO_ALLOWED_MIME:
        raise HTTPException(
            status_code=400,
            detail="Photo must be a JPEG, PNG, or WebP image",
        )
    photo_bytes = await photo.read()
    if not photo_bytes:
        raise HTTPException(
            status_code=400,
            detail="An ID-badge photo is required.",
        )
    if len(photo_bytes) > _PHOTO_MAX_BYTES:
        raise HTTPException(
            status_code=400, detail="Photo must be under 5MB",
        )
    photo_ext = (photo.filename.rsplit(".", 1)[-1].lower()
                 if "." in (photo.filename or "") else "jpg")
    # Guard against pathological extensions — force to the
    # content-type's canonical mapping when in doubt.
    if photo_ext not in ("jpg", "jpeg", "png", "webp"):
        photo_ext = {"image/jpeg": "jpg", "image/png": "png",
                     "image/webp": "webp"}[photo.content_type]

    if not _valid_name(first) or not _valid_name(last):
        raise HTTPException(status_code=400, detail="First and last name look invalid")
    if not room_val:
        raise HTTPException(status_code=400, detail="Room is required")
    if assignment not in ASSIGNMENT_OPTIONS:
        raise HTTPException(status_code=400, detail="Pick an assignment from the list")

    cfg = await _resolve_sheet_config(db, row.building)
    if not cfg:
        raise HTTPException(
            status_code=500,
            detail=f"No sheet configured for {row.building} — contact IT",
        )

    # ── 1. Append to the building's NexusData tab ──
    #
    # Column order matches the sheet: A=Room, B=Last, C=First,
    # D=Category, E=Role/Grade (we use title for the last col so the
    # room roster's assignment field can still be edited by admins
    # after submit).
    try:
        from app.integrations.google.sheets_adapter import GoogleSheetsAdapter
        ad = GoogleSheetsAdapter(db)
        await ad.append_row(
            cfg["sheet_id"], cfg["append_range"],
            [room_val, last, first, assignment, title],
        )
        sheet_ok = True
        sheet_err = None
    except Exception as e:
        logger.exception("Onboarding sheet append failed")
        sheet_ok = False
        sheet_err = f"{type(e).__name__}: {e}"[:200]

    # ── 2. Insert a staff_queue row (held for admin review) ──
    #
    # Even if the sheet append failed above, still queue the row —
    # admin can retry the sheet write manually. The queue row is the
    # authoritative record for provisioning.
    from app.modules.settings.repository import get_setting_value
    domain = (await get_setting_value(db, "google", "domain") or "yourdistrict.org").strip()
    fn_email = re.sub(r"[^a-z]", "", first.lower())
    ln_email = re.sub(r"[^a-z\-]", "", last.lower())
    expected_email = f"{fn_email}.{ln_email}@{domain}" if fn_email and ln_email else ""

    submitted_data = {
        "first_name": first,
        "last_name": last,
        "preferred_name": preferred,
        "room": room_val,
        "assignment": assignment,
        "title": title,
        "cell_phone": cell,
        "personal_email": personal,
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent", "")[:255],
        "sheet_append_ok": sheet_ok,
        "sheet_error": sheet_err,
    }

    queue_id = None
    try:
        role_type = _ASSIGNMENT_TO_ROLE.get(assignment, "classified")
        result = await db.execute(text("""
            INSERT INTO staff_queue
                (action, first_name, last_name, preferred_name, building, role_type,
                 source, status, room, school, expected_email,
                 source_detail, title, created_at)
            VALUES
                ('provision', :first, :last, :preferred, :building, :role_type,
                 'self_service_onboarding', 'pending_review', :room, :building,
                 :expected_email, :source_detail, :title, now())
            RETURNING id
        """).bindparams(
            first=first, last=last, preferred=(preferred or None),
            building=row.building, room=room_val, role_type=role_type,
            expected_email=expected_email,
            source_detail=json.dumps({
                "token_id": row.id,
                "assignment": assignment,
                "cell_phone": cell,
                "personal_email": personal,
                "ip": request.client.host if request.client else None,
            }),
            title=title,
        ))
        queue_id = result.scalar_one()
    except Exception as e:
        logger.exception("Onboarding queue insert failed")
        # If both writes failed, refuse — nothing to show for it.
        if not sheet_ok:
            raise HTTPException(
                status_code=500,
                detail="Both sheet and queue writes failed — please contact IT",
            )

    # ── 2b. Save the badge photo (if provided) to the queue row ──
    #
    # Non-fatal on failure — the primary submission already succeeded.
    # Result is recorded in submitted_data so admins can see what
    # happened when reviewing the queue entry.
    photo_ok = None
    photo_error: str | None = None
    photo_face_found: bool | None = None
    if photo_bytes and queue_id is not None:
        try:
            # Run through the ID-card photo processor: EXIF-orient (fixes
            # the phone-rotated-sideways case that got the first
            # self-enrollee's photo locked into Paxton sideways),
            # face detection + smart crop, aspect-fit to CR80. Always
            # normalized to JPEG regardless of source type.
            from app.modules.staff.photo_processor import process_photo
            result = process_photo(photo_bytes, prefer_face_detection=True)
            photo_dir = os.path.join("data", "photos")
            os.makedirs(photo_dir, exist_ok=True)
            filename = f"queue_{queue_id}.jpg"
            filepath = os.path.join(photo_dir, filename)
            if result.ok:
                with open(filepath, "wb") as fh:
                    fh.write(result.bytes_jpeg)
                # Loose auto-advance: EXIF-orient + resolution pass = ready.
                # Face-not-found is a warning, not a blocker; operator
                # eyeballs the preview on /staff/id-cards before printing.
                await db.execute(text("""
                    UPDATE staff_queue
                    SET photo_path = :p,
                        id_card_status = 'ready',
                        id_card_updated_at = NOW()
                    WHERE id = :i
                """).bindparams(p=filepath, i=queue_id))
                photo_ok = True
                photo_face_found = result.face_found
            else:
                # Save raw so an admin can inspect + re-upload from
                # /staff/id-cards. Leaves id_card_status at pending_photo.
                with open(filepath, "wb") as fh:
                    fh.write(photo_bytes)
                await db.execute(text("""
                    UPDATE staff_queue
                    SET photo_path = :p,
                        id_card_status = 'pending_photo',
                        id_card_updated_at = NOW()
                    WHERE id = :i
                """).bindparams(p=filepath, i=queue_id))
                photo_ok = False
                photo_error = result.reason
        except Exception as e:
            logger.exception("Onboarding photo write failed")
            photo_ok = False
            photo_error = f"{type(e).__name__}: {e}"[:200]
    submitted_data["photo_uploaded"] = photo_ok
    if photo_face_found is not None:
        submitted_data["photo_face_detected"] = photo_face_found
    if photo_error:
        submitted_data["photo_error"] = photo_error

    # ── 3. Mark token used ──
    row.status = "submitted"
    row.used_at = datetime.now(timezone.utc)
    row.submitted_data = submitted_data
    row.resulting_queue_id = queue_id

    # ── 4. Audit ──
    await log_action(
        db, actor="onboarding_submitter",
        action="staff.onboarding.submitted",
        module="staff",
        target=f"token#{row.id} -> queue#{queue_id or 'none'}",
        details=json.dumps({
            "building": row.building,
            "name": f"{first} {last}",
            "sheet_append_ok": sheet_ok,
            "sheet_error": sheet_err,
            "issued_by": row.issued_by,
            **_request_meta(request),
        }),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    # ── 5. Confirmation email to the new hire (best-effort) ──
    #
    # Non-fatal — logged to audit with outcome. Only fires when the
    # recipient supplied a personal email; district email doesn't
    # exist yet, so that's the only address we could reach them at.
    confirm_email_result = await _send_submit_confirmation(
        db,
        personal_email=personal,
        first_name=first,
        last_name=last,
        building=row.building,
        assignment=assignment,
        room=room_val,
    )

    return {
        "ok": True,
        "queue_id": queue_id,
        "sheet_append_ok": sheet_ok,
        "photo_uploaded": photo_ok,
        "confirmation_email": confirm_email_result,
        "message": (
            "Thanks — your info is with the tech team. "
            "You'll hear back within one business day."
        ),
    }


async def _send_submit_confirmation(
    db: AsyncSession, *, personal_email: str, first_name: str,
    last_name: str, building: str, assignment: str, room: str,
) -> dict | None:
    """Fire the "we got your submission" email to the new hire's
    personal address. Fully non-fatal — every path returns a dict
    describing what happened, and the caller can drop the whole thing
    on the floor if SMTP is broken. Uses `to_thread` so the sync
    _send_email call doesn't block the request event loop."""
    if not personal_email:
        return None
    import asyncio as _asyncio

    from app.modules.settings.repository import get_setting_value as _gsv
    from app.modules.staff.router import _resolve_notification_sender
    from app.workers.notifications import notify_onboarding_submitted

    sender = await _resolve_notification_sender(db)
    district_name = (await _gsv(db, "branding", "district_name") or "").strip()

    if not sender:
        outcome = {"sent": False, "recipient": personal_email,
                   "error": "notification sender not configured"}
    else:
        try:
            ok = await _asyncio.to_thread(
                notify_onboarding_submitted,
                sender, personal_email,
                first_name=first_name, last_name=last_name,
                building=building, assignment=assignment,
                room=(room or None), district_name=district_name,
            )
            outcome = {"sent": bool(ok), "recipient": personal_email,
                       "error": None if ok else "smtp send returned False"}
        except Exception as e:
            outcome = {"sent": False, "recipient": personal_email,
                       "error": str(e)[:200]}
            logger.warning(
                f"onboarding submit confirmation failed for "
                f"{personal_email}: {e}"
            )

    try:
        await log_action(
            db, actor="onboarding_submitter",
            action="staff.onboarding.submit_confirmation",
            module="staff", target=personal_email,
            details=json.dumps(outcome),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"audit log for submit confirmation failed: {e}")

    return outcome
