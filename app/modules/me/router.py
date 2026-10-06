"""Per-user profile / personal settings.

Currently scoped to alert recipient linking + push subscription management
(the things a tech needs from their own phone). This module is the right
place for any future per-user preferences.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action
from app.db.engine import get_db
from app.db.models import User
from app.modules.alerts.helpers import norm_mac
from app.modules.alerts.models import TechPushSubscription
from app.modules.voice.models import VoiceRecipient
from app.policies.engine import get_user_permissions, check_permission, require_action
from app.policies.page_context import build_page_modules

logger = logging.getLogger(__name__)
router = APIRouter(tags=["me"])
templates = Jinja2Templates(directory="app/templates")


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("/me", response_class=HTMLResponse)
async def me_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_action("alerts.tech.view")),
):
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    return templates.TemplateResponse("me.html", {"request": request, "user": user, "modules": modules})


# ── API ───────────────────────────────────────────────────────────────────────

@router.get("/api/me/profile")
async def get_my_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_action("alerts.tech.view")),
):
    """Return current user's profile + linked recipient details."""
    recipient = None
    push_sub_count = 0
    if user.voice_recipient_id:
        r = (await db.execute(
            select(VoiceRecipient).where(VoiceRecipient.id == user.voice_recipient_id)
        )).scalar_one_or_none()
        if r:
            push_sub_count = len((await db.execute(
                select(TechPushSubscription).where(TechPushSubscription.recipient_id == r.id)
            )).scalars().all())
            recipient = {
                "id": r.id,
                "label": r.label,
                "extension": r.extension,
                "number_e164": r.number_e164,
                "mac_address": r.mac_address,
                "home_lat": r.home_lat,
                "home_lon": r.home_lon,
                "active": r.active,
                "voice_enabled": r.voice_enabled,
                "push_enabled": r.push_enabled,
                "severity_threshold": r.severity_threshold,
                "quiet_hours_start": r.quiet_hours_start.strftime("%H:%M") if r.quiet_hours_start else None,
                "quiet_hours_end": r.quiet_hours_end.strftime("%H:%M") if r.quiet_hours_end else None,
                "quiet_hours_override_severity": r.quiet_hours_override_severity,
                "quiet_weekends": r.quiet_weekends,
                "priority_order": r.priority_order,
                "push_subscriptions": push_sub_count,
            }
        else:
            # Stale pointer (recipient deleted) — clear it
            user.voice_recipient_id = None
            await db.commit()

    return {
        "email": user.email,
        "name": user.name,
        "voice_recipient_id": user.voice_recipient_id,
        "recipient": recipient,
        "out_of_office_until": (
            user.out_of_office_until.isoformat() if user.out_of_office_until else None
        ),
    }


@router.get("/api/me/recipient-options")
async def list_recipient_options(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_action("alerts.tech.view")),
):
    """Active recipients available for self-claim — excludes recipients
    already linked to a different user. Admins (alerts.tech.manage) see all.
    """
    perms = await get_user_permissions(db, user.id)
    is_admin = check_permission(perms, "alerts.tech.manage")

    recipients = (await db.execute(
        select(VoiceRecipient)
        .where(VoiceRecipient.active == True)
        .order_by(VoiceRecipient.priority_order.asc())
    )).scalars().all()

    if is_admin:
        return [{"id": r.id, "label": r.label} for r in recipients]

    # Non-admins: filter out recipients claimed by someone else.
    claimed_rows = (await db.execute(
        select(User.voice_recipient_id, User.id).where(User.voice_recipient_id.is_not(None))
    )).all()
    claimed_by_other = {
        rid for rid, uid in claimed_rows if uid != user.id
    }
    return [
        {"id": r.id, "label": r.label}
        for r in recipients
        if r.id not in claimed_by_other
    ]


@router.patch("/api/me/profile")
async def update_my_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_action("alerts.tech.view")),
):
    """Set or clear the voice_recipient_id link for the current user.

    Non-admins can only claim a recipient that no other user has linked.
    Admins (alerts.tech.manage) may claim anything (e.g. to test or rebind
    on someone's behalf). All link/unlink events are audited.
    """
    data = await request.json()
    if "voice_recipient_id" not in data:
        return {"status": "ok", "voice_recipient_id": user.voice_recipient_id}

    rid = data["voice_recipient_id"]
    old = user.voice_recipient_id

    if rid is None:
        user.voice_recipient_id = None
        await db.commit()
        await log_action(db, actor=user.email, action="me.unlink_recipient",
                         module="me", target=f"voice_recipient:{old}" if old else None,
                         details=f"unlinked recipient_id={old}")
        return {"status": "ok", "voice_recipient_id": None}

    try:
        rid = int(rid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="voice_recipient_id must be an integer")

    r = (await db.execute(select(VoiceRecipient).where(VoiceRecipient.id == rid))).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Recipient not found")

    perms = await get_user_permissions(db, user.id)
    is_admin = check_permission(perms, "alerts.tech.manage")

    if not is_admin:
        # Reject if already claimed by a different user
        claimed_by = (await db.execute(
            select(User.id, User.email)
            .where(User.voice_recipient_id == rid, User.id != user.id)
            .limit(1)
        )).first()
        if claimed_by:
            raise HTTPException(
                status_code=409,
                detail="Recipient already linked to another user — ask an admin to unlink it first",
            )

    user.voice_recipient_id = rid
    await db.commit()
    await log_action(db, actor=user.email, action="me.link_recipient",
                     module="me", target=f"voice_recipient:{rid}",
                     details=f"linked recipient_id={rid} (previous={old}, admin={is_admin})")
    return {"status": "ok", "voice_recipient_id": rid}


def _parse_optional_float(val, field: str) -> float | None:
    """Coerce form-style input to float, returning None for empty.
    Raises 400 (not 500) on bad input."""
    if val in (None, ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")


@router.patch("/api/me/recipient")
async def update_my_recipient(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_action("alerts.tech.view")),
):
    """Update self-service fields on the recipient linked to this user.

    Whitelisted fields only — admin-controlled fields (label, priority, active,
    severity_threshold) are not editable here; those go through Settings → Alerts.
    """
    if not user.voice_recipient_id:
        raise HTTPException(status_code=400, detail="No recipient linked to your profile")

    r = (await db.execute(
        select(VoiceRecipient).where(VoiceRecipient.id == user.voice_recipient_id)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Linked recipient not found")

    # Defense in depth: even though we keep the user→recipient pointer in sync,
    # confirm that no other user has also claimed this recipient. A non-admin
    # writing here should always be the unique claimant.
    perms = await get_user_permissions(db, user.id)
    is_admin = check_permission(perms, "alerts.tech.manage")
    if not is_admin:
        other = (await db.execute(
            select(User.id).where(User.voice_recipient_id == r.id, User.id != user.id).limit(1)
        )).scalar_one_or_none()
        if other:
            raise HTTPException(status_code=409, detail="Recipient is also linked to another user")

    data = await request.json()
    changed = []

    SEVERITIES = {"critical", "high", "medium", "info"}

    def _parse_hhmm(v, name):
        from datetime import time as _time
        if v in (None, ""):
            return None
        try:
            hh, mm = v.split(":")
            return _time(int(hh), int(mm))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{name} must be HH:MM")

    if "number_e164" in data:
        v = (data.get("number_e164") or "").strip()
        if v and len(v) > 20:
            raise HTTPException(status_code=400, detail="number_e164 too long")
        r.number_e164 = v or None
        changed.append("number_e164")
    if "extension" in data:
        v = (data.get("extension") or "").strip()
        if v and len(v) > 10:
            raise HTTPException(status_code=400, detail="extension too long")
        r.extension = v or None
        changed.append("extension")
    if "mac_address" in data:
        try:
            r.mac_address = norm_mac(data.get("mac_address"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        changed.append("mac_address")
    if "home_lat" in data:
        r.home_lat = _parse_optional_float(data.get("home_lat"), "home_lat")
        changed.append("home_lat")
    if "home_lon" in data:
        r.home_lon = _parse_optional_float(data.get("home_lon"), "home_lon")
        changed.append("home_lon")

    if "severity_threshold" in data:
        v = (data.get("severity_threshold") or "").strip().lower()
        if v not in SEVERITIES:
            raise HTTPException(status_code=400, detail=f"severity_threshold must be one of {sorted(SEVERITIES)}")
        r.severity_threshold = v
        changed.append("severity_threshold")
    if "quiet_hours_start" in data:
        r.quiet_hours_start = _parse_hhmm(data.get("quiet_hours_start"), "quiet_hours_start")
        changed.append("quiet_hours_start")
    if "quiet_hours_end" in data:
        r.quiet_hours_end = _parse_hhmm(data.get("quiet_hours_end"), "quiet_hours_end")
        changed.append("quiet_hours_end")
    if "quiet_hours_override_severity" in data:
        v = (data.get("quiet_hours_override_severity") or "").strip().lower()
        if v and v not in SEVERITIES:
            raise HTTPException(status_code=400, detail=f"quiet_hours_override_severity must be one of {sorted(SEVERITIES)} or blank")
        r.quiet_hours_override_severity = v or None
        changed.append("quiet_hours_override_severity")
    if "quiet_weekends" in data:
        r.quiet_weekends = bool(data.get("quiet_weekends"))
        changed.append("quiet_weekends")
    if "priority_order" in data:
        try:
            po = int(data.get("priority_order"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="priority_order must be an integer")
        if po < 0 or po > 9999:
            raise HTTPException(status_code=400, detail="priority_order must be between 0 and 9999")
        r.priority_order = po
        changed.append("priority_order")
    if "voice_enabled" in data:
        r.voice_enabled = bool(data.get("voice_enabled"))
        changed.append("voice_enabled")
    if "push_enabled" in data:
        r.push_enabled = bool(data.get("push_enabled"))
        changed.append("push_enabled")

    if not r.number_e164 and not r.extension:
        raise HTTPException(status_code=400, detail="Must have a phone number or extension")

    await db.commit()
    await log_action(db, actor=user.email, action="me.update_recipient",
                     module="me", target=f"voice_recipient:{r.id}",
                     details=f"updated fields: {','.join(changed)}")
    return {"status": "ok"}



# ── Per-user UI preferences ──────────────────────────────────────────────
# Flat JSON dict on ``users.preferences``. Each feature owns one key.
# PATCH merges supplied keys with the existing dict so independent
# features (label printer, future settings) don't clobber each other.
#
# Known keys (so far):
#   default_label_printer_id : str — chromebook label printer dropdown
#                                    auto-select for this user.

from app.auth.session import get_current_user as _get_current_user

_ALLOWED_PREFERENCE_KEYS: set[str] = {
    "default_label_printer_id",
}


@router.get("/api/me/preferences")
async def get_my_preferences(user: User = Depends(_get_current_user)):
    """Return this user's preferences dict (empty if unset)."""
    return user.preferences or {}


@router.patch("/api/me/preferences")
async def update_my_preferences(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_get_current_user),
):
    """Merge supplied keys into the user's preferences. Pass ``null``
    for a key to clear it. Unknown keys are rejected."""
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    unknown = set(payload.keys()) - _ALLOWED_PREFERENCE_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown preference keys: {sorted(unknown)}",
        )
    prefs = dict(user.preferences or {})
    for k, v in payload.items():
        if v is None:
            prefs.pop(k, None)
        else:
            prefs[k] = v
    user.preferences = prefs
    await db.commit()
    return {"status": "ok", "preferences": prefs}


@router.patch("/api/me/ooo")
async def update_my_ooo(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_get_current_user),
):
    """Set or clear the self-set out-of-office date. The routing-rule
    resolver skips users whose OOO date is on or after today, walking
    to the next entry in the rule's backup chain. Pass ``until: null``
    to clear. Past dates are accepted but treated as cleared."""
    from datetime import date as _date
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    raw = payload.get("until")
    if raw is None or raw == "":
        new_value = None
    else:
        try:
            new_value = _date.fromisoformat(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="until must be YYYY-MM-DD or null")
        if new_value < _date.today():
            new_value = None  # past-dated means "already back"
    user.out_of_office_until = new_value
    await db.commit()
    await log_action(
        db, actor=user.email, action="me.ooo_set",
        module="me", target=None,
        details=f"until={new_value.isoformat() if new_value else 'cleared'}",
    )
    return {
        "status": "ok",
        "out_of_office_until": new_value.isoformat() if new_value else None,
    }
