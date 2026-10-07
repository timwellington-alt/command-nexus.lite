"""Local-auth router — secondary login path alongside Google SSO.

Gated behind the `LOCAL_AUTH_ENABLED` env flag. When off, this
module's routes aren't registered at all so there's no attack
surface for districts that don't want local accounts.

User flow:
  - GET /auth/local        — login form (also served from /login tab)
  - POST /auth/local/login — email + password, sets session cookie
  - GET  /auth/local/logout — clears session, redirects to /

Admin flow (SETTINGS → Local accounts):
  - GET    /api/local-users         — list
  - POST   /api/local-users         — create { email, display_name, password, is_admin }
  - PATCH  /api/local-users/{id}    — update { active?, is_admin?, display_name?, password? }
  - DELETE /api/local-users/{id}    — delete

Password storage: argon2id via argon2-cffi (OWASP-recommended).
Rate limiting: per-email Redis counter, lockout after N failures.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import get_db
from app.audit.service import log_action
from app.auth.session_store import rotate_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/local", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


# Lockout tuning. 5 failures within 15 min → 15 min lockout. Operator
# can override via LOCAL_AUTH_LOCKOUT_* env vars if their policy differs.
import os as _os
_MAX_FAILURES = int(_os.environ.get("LOCAL_AUTH_LOCKOUT_FAILURES", "5"))
_WINDOW_SEC   = int(_os.environ.get("LOCAL_AUTH_LOCKOUT_WINDOW_SEC", "900"))


def _hasher():
    """Lazy-init so a build that disables local auth doesn't pay the
    argon2-cffi import cost."""
    from argon2 import PasswordHasher
    return PasswordHasher()  # defaults are OWASP-recommended


async def _redis():
    import redis.asyncio as aioredis
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def _bump_failures(email: str) -> int:
    """Return the current failure count for this email AFTER incrementing."""
    key = f"local_auth:fail:{email.lower()}"
    r = await _redis()
    try:
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, _WINDOW_SEC)
        count, _ = await pipe.execute()
        return int(count)
    finally:
        await r.aclose()


async def _is_locked(email: str) -> bool:
    key = f"local_auth:fail:{email.lower()}"
    r = await _redis()
    try:
        val = await r.get(key)
        return int(val or 0) >= _MAX_FAILURES
    finally:
        await r.aclose()


async def _clear_failures(email: str) -> None:
    key = f"local_auth:fail:{email.lower()}"
    r = await _redis()
    try:
        await r.delete(key)
    finally:
        await r.aclose()


@router.post("/login")
async def local_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Handle a local-auth login form POST. On success rotates the
    session cookie (same as Google callback) so downstream middleware
    treats this user identically to a Google-authenticated one."""
    # Belt-and-suspenders CSRF: middleware exempts this route (no session
    # yet, no custom header possible) so enforce Origin/Referer here.
    # Config switches to permissive mode when DOMAIN is still a placeholder
    # (first-boot UX) — see origin_check_permissive in config.py.
    from urllib.parse import urlparse
    settings = get_settings()
    allowed = settings.allowed_domains
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if source and not settings.origin_check_permissive:
        host = urlparse(source).hostname
        if host and host not in allowed and host != "localhost":
            logger.warning("local_login: cross-origin POST from %s", source)
            raise HTTPException(status_code=403, detail="cross-origin login POST rejected")

    email = (email or "").strip().lower()
    if not email or not password:
        return RedirectResponse(url="/?err=local_missing", status_code=303)

    if await _is_locked(email):
        logger.warning("local_login: locked out — %s", email)
        return RedirectResponse(url="/?err=local_locked", status_code=303)

    row = (await db.execute(text("""
        SELECT id, email, display_name, pw_hash, is_admin, active,
               must_change_password
        FROM local_users
        WHERE lower(email) = :e
        LIMIT 1
    """).bindparams(e=email))).mappings().first()

    valid = False
    if row and row["active"]:
        try:
            _hasher().verify(row["pw_hash"], password)
            valid = True
        except Exception:
            valid = False

    if not valid:
        count = await _bump_failures(email)
        logger.warning("local_login: failed for %s (count=%d)", email, count)
        await log_action(
            db, actor=email, action="auth.local.failed",
            module="auth", target=email,
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()
        err = "local_locked" if count >= _MAX_FAILURES else "local_bad"
        return RedirectResponse(url=f"/?err={err}", status_code=303)

    await _clear_failures(email)
    await db.execute(text("""
        UPDATE local_users SET last_login = NOW() WHERE id = :id
    """).bindparams(id=row["id"]))
    await log_action(
        db, actor=email, action="auth.local.ok",
        module="auth", target=email,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    # Issue the session cookie exactly the way the Google callback does
    # so downstream middleware treats these users identically.
    settings = get_settings()
    import redis.asyncio as aioredis
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        from app.auth.session_store import SESSION_TTL
        old = request.cookies.get("session")
        new_cookie = await rotate_session(
            r, old,
            new_data={
                "user_email": row["email"],
                "user_display_name": row["display_name"] or row["email"],
                "auth_method": "local",
                "must_change_password": bool(row["must_change_password"]),
            },
            secret_key=settings.app_secret_key,
            max_age=int(SESSION_TTL.total_seconds()),
        )
    finally:
        await r.aclose()

    # If this account must change its password (default-admin bootstrap
    # or admin-triggered reset), land on the change page instead of
    # dashboard. Middleware allows that specific path for authenticated
    # users with the flag set.
    dest = "/auth/local/change-password" if row["must_change_password"] else "/dashboard"
    resp = RedirectResponse(url=dest, status_code=303)
    resp.set_cookie(
        "session", new_cookie,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True, samesite="lax",
        secure=settings.is_production, path="/",
    )
    return resp


@router.get("/change-password", response_class=HTMLResponse)
async def local_change_password_form(request: Request):
    """Form shown when must_change_password=true. Any authenticated
    local-auth user can use this to change their own password; it's
    FORCED on first login after bootstrap or an admin reset."""
    if not request.session.get("user_email"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("local_change_password.html", {
        "request": request,
        "err": request.query_params.get("err"),
    })


@router.post("/change-password")
async def local_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    email = request.session.get("user_email")
    if not email:
        return RedirectResponse(url="/", status_code=303)
    if new_password != new_password_confirm:
        return RedirectResponse(url="/auth/local/change-password?err=mismatch", status_code=303)
    if len(new_password) < 12:
        return RedirectResponse(url="/auth/local/change-password?err=too_short", status_code=303)
    if new_password == current_password:
        return RedirectResponse(url="/auth/local/change-password?err=same", status_code=303)

    row = (await db.execute(text(
        "SELECT id, pw_hash FROM local_users WHERE lower(email) = :e"
    ).bindparams(e=email.lower()))).mappings().first()
    if not row:
        # Not a local user — they got here via Google SSO. Punt to dashboard.
        return RedirectResponse(url="/dashboard", status_code=303)
    try:
        _hasher().verify(row["pw_hash"], current_password)
    except Exception:
        return RedirectResponse(url="/auth/local/change-password?err=bad_current", status_code=303)

    await db.execute(text("""
        UPDATE local_users
        SET pw_hash = :p, must_change_password = false
        WHERE id = :id
    """).bindparams(p=_hasher().hash(new_password), id=row["id"]))
    await log_action(
        db, actor=email, action="auth.local.password_changed",
        module="auth", target=email,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    # Clear the session flag so middleware stops redirecting back here
    try:
        request.session.pop("must_change_password", None)
    except Exception:
        pass
    return RedirectResponse(url="/dashboard", status_code=303)


# ─── Admin management (requires logged-in admin) ──────────────────
# Mounted at /api/local-users via admin_router. Separate from the
# login path to keep the auth-not-required set tight.

from app.policies.engine import require_action
from app.db.models import User

admin_router = APIRouter(prefix="/api/local-users", tags=["local-auth-admin"])


@admin_router.get("")
async def list_local_users(
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(text("""
        SELECT id, email, display_name, is_admin, active, created_at,
               created_by, last_login, must_change_password
        FROM local_users
        ORDER BY email
    """))).mappings().all()
    return {"users": [dict(r) for r in rows]}


@admin_router.post("")
async def create_local_user(
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    display = (body.get("display_name") or "").strip() or email
    is_admin = bool(body.get("is_admin", False))
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="valid email required")
    if len(pw) < 12:
        raise HTTPException(status_code=400, detail="password must be ≥ 12 chars")

    pw_hash = _hasher().hash(pw)
    try:
        await db.execute(text("""
            INSERT INTO local_users (email, display_name, pw_hash, is_admin, created_by)
            VALUES (:e, :d, :p, :a, :b)
        """).bindparams(e=email, d=display, p=pw_hash, a=is_admin, b=user.email))
        await log_action(
            db, actor=user.email, action="auth.local.user_created",
            module="auth", target=email,
            details=f'is_admin={is_admin}',
        )
        await db.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"create failed: {str(e)[:120]}")
    return {"ok": True, "email": email}


@admin_router.patch("/{user_id}")
async def update_local_user(
    user_id: int,
    request: Request,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    sets = []
    params: dict = {"id": user_id}
    if "active" in body:
        sets.append("active = :active")
        params["active"] = bool(body["active"])
    if "is_admin" in body:
        sets.append("is_admin = :is_admin")
        params["is_admin"] = bool(body["is_admin"])
    if "display_name" in body:
        sets.append("display_name = :display")
        params["display"] = (body["display_name"] or "").strip() or None
    if "password" in body:
        pw = body["password"] or ""
        if len(pw) < 12:
            raise HTTPException(status_code=400, detail="password must be ≥ 12 chars")
        sets.append("pw_hash = :pw")
        params["pw"] = _hasher().hash(pw)
        # Admin-triggered password reset → user must change on next login
        # unless the admin explicitly says they're setting a known value.
        if not body.get("skip_force_change"):
            sets.append("must_change_password = true")
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    await db.execute(text(
        f"UPDATE local_users SET {', '.join(sets)} WHERE id = :id"
    ).bindparams(**params))
    await log_action(
        db, actor=user.email, action="auth.local.user_updated",
        module="auth", target=str(user_id),
        details=','.join(f'{k}' for k in body.keys() if k != 'password') + (' + password' if 'password' in body else ''),
    )
    await db.commit()
    return {"ok": True}


@admin_router.delete("/{user_id}")
async def delete_local_user(
    user_id: int,
    user: User = Depends(require_action("staff.provision.execute")),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(text(
        "SELECT email FROM local_users WHERE id = :id"
    ).bindparams(id=user_id))).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    await db.execute(text(
        "DELETE FROM local_users WHERE id = :id"
    ).bindparams(id=user_id))
    await log_action(
        db, actor=user.email, action="auth.local.user_deleted",
        module="auth", target=row["email"],
    )
    await db.commit()
    return {"ok": True}


@router.get("/logout")
async def local_logout(request: Request):
    """Logs out a local-auth session. Same shape as Google's logout."""
    settings = get_settings()
    import redis.asyncio as aioredis
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        from app.auth.session_store import _unsign_session_id
        old = request.cookies.get("session")
        if old:
            sid = _unsign_session_id(old, settings.app_secret_key)
            if sid:
                await r.delete(f"session:{sid}")
    finally:
        await r.aclose()
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("session", path="/")
    return resp
