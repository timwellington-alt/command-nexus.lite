"""Per-user profile — Nexus-lite scope.

Endpoints:
  GET  /me                    → HTML profile page
  GET  /api/me/profile        → identity + security status + prefs
  PATCH /api/me/profile       → update display name / OOO
  POST /api/me/totp-reset     → clear my own TOTP so next login re-enrolls
                               (local-auth only; local-auth users may
                               wipe their OWN TOTP seed to re-pair with
                               a different authenticator app)
"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action
from app.db.engine import get_db
from app.db.models import User
from app.policies.engine import get_user_permissions
from app.policies.page_context import build_page_modules

logger = logging.getLogger(__name__)
router = APIRouter(tags=["me"])
templates = Jinja2Templates(directory="app/templates")


async def _get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Minimal 'any authenticated user' dependency — no permission gate."""
    email = (request.session or {}).get("user_email")
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    row = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if not row or not row.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return row


@router.get("/me", response_class=HTMLResponse)
async def me_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_get_current_user),
):
    permissions = await get_user_permissions(db, user.id)
    modules = build_page_modules(permissions)
    return templates.TemplateResponse("me.html", {
        "request": request, "user": user, "modules": modules,
    })


@router.get("/api/me/profile")
async def get_my_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_get_current_user),
):
    """Return the current user's identity + security state."""
    auth_method = (request.session or {}).get("auth_method", "google")
    is_local = auth_method == "local"

    # Local-auth specific: TOTP enrollment state
    totp_enrolled_at = None
    if is_local:
        row = (await db.execute(text(
            "SELECT totp_verified_at FROM local_users WHERE lower(email) = lower(:e)"
        ).bindparams(e=user.email))).first()
        if row and row[0]:
            totp_enrolled_at = row[0].isoformat()

    # Roles for display
    roles = (await db.execute(text("""
        SELECT r.name FROM user_roles ur
        JOIN roles r ON r.id = ur.role_id
        WHERE ur.user_id = :u
        ORDER BY r.name
    """).bindparams(u=user.id))).scalars().all()

    return {
        "email": user.email,
        "name": user.name,
        "auth_method": auth_method,
        "is_local": is_local,
        "totp_enrolled_at": totp_enrolled_at,
        "roles": list(roles),
        "out_of_office_until": (
            user.out_of_office_until.isoformat() if user.out_of_office_until else None
        ),
    }


@router.patch("/api/me/profile")
async def update_my_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_get_current_user),
):
    """Update the fields the user may change themselves: display name
    and out-of-office date. Email and roles are admin-managed only."""
    body = await request.json()
    changes = {}

    if "name" in body:
        new_name = (body["name"] or "").strip()
        if new_name != (user.name or ""):
            user.name = new_name or None
            changes["name"] = new_name

    if "out_of_office_until" in body:
        v = body["out_of_office_until"]
        if v in (None, "", "null"):
            if user.out_of_office_until is not None:
                user.out_of_office_until = None
                changes["out_of_office_until"] = None
        else:
            try:
                parsed = date.fromisoformat(v)
            except Exception:
                raise HTTPException(status_code=400, detail="out_of_office_until must be YYYY-MM-DD")
            if user.out_of_office_until != parsed:
                user.out_of_office_until = parsed
                changes["out_of_office_until"] = parsed.isoformat()

    # Local-auth display name — mirror into local_users so Local Accounts
    # panel + login greeting stay in sync.
    if "name" in changes:
        try:
            await db.execute(text(
                "UPDATE local_users SET display_name = :n WHERE lower(email) = lower(:e)"
            ).bindparams(n=changes["name"], e=user.email))
        except Exception:
            pass

    if changes:
        await log_action(
            db, actor=user.email, action="me.profile.update",
            module="me", target=user.email,
            details=f"changes={sorted(changes)}",
            ip_address=request.client.host if request.client else None,
        )
    await db.commit()
    return {"status": "ok", "changes": changes}


@router.post("/api/me/totp-reset")
async def reset_my_totp(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_get_current_user),
):
    """Clear the current user's TOTP secret so the next login forces
    re-enrollment. Useful if they lose access to their authenticator
    app and want to pair a new one. Local-auth only."""
    auth_method = (request.session or {}).get("auth_method", "")
    if auth_method != "local":
        raise HTTPException(status_code=400, detail="TOTP reset is for local-auth accounts only")

    await db.execute(text("""
        UPDATE local_users
        SET totp_secret = NULL, totp_verified_at = NULL
        WHERE lower(email) = lower(:e)
    """).bindparams(e=user.email))
    await log_action(
        db, actor=user.email, action="me.totp_reset",
        module="me", target=user.email,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    # The current session was authenticated with the old TOTP — kill
    # it so the user is forced through re-enrollment on next login.
    try:
        request.session.clear()
    except Exception:
        pass
    return {"status": "ok", "redirect": "/auth/local/logout"}
