"""
Google OAuth login flow.

Handles login redirect, callback, session creation, and logout.
- OAuth state stored in Redis with 5-min TTL
- Google Group membership checked and synced to roles on every login
- Sessions stored in Redis (server-side invalidation)
"""

import logging
import time
import urllib.parse
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import get_db
from app.db.models import User
from app.audit.service import log_action
from app.auth.oauth_state import create_oauth_state, validate_oauth_state
from app.auth.group_sync import sync_user_roles

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


@router.get("/login")
async def login(request: Request):
    """Redirect to Google OAuth consent screen."""
    settings = get_settings()

    # Generate state in Redis with 5-min TTL
    state = await create_oauth_state()

    scheme = request.headers.get("x-forwarded-proto", "https" if settings.is_production else "http")
    # Use the request host if it's in the allowed domains list (supports Cloudflare tunnel)
    req_host = request.headers.get("host", settings.domain).split(":")[0]
    host_with_port = request.headers.get("host", settings.domain)
    if req_host in settings.allowed_domains and req_host != settings.domain.split(":")[0]:
        # External domain (Cloudflare) — always HTTPS, no port
        domain = req_host
        scheme = "https"
    else:
        domain = settings.domain
    redirect_uri = f"{scheme}://{domain}/auth/callback"
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "hd": settings.google_domain,
        "prompt": "select_account",
    }
    url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=url)


@router.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Handle Google OAuth callback."""
    settings = get_settings()

    # Validate state from Redis (consumed on use)
    if not await validate_oauth_state(state):
        logger.warning("OAuth state invalid or expired")
        return RedirectResponse(url="/?error=state_mismatch")

    if error:
        logger.warning(f"OAuth error: {error}")
        safe_error = urllib.parse.quote(error, safe="")
        return RedirectResponse(url=f"/?error={safe_error}")

    if not code:
        return RedirectResponse(url="/?error=no_code")

    # Exchange code for tokens — redirect_uri must match the one sent during login
    scheme = request.headers.get("x-forwarded-proto", "https" if settings.is_production else "http")
    req_host = request.headers.get("host", settings.domain).split(":")[0]
    if req_host in settings.allowed_domains and req_host != settings.domain.split(":")[0]:
        domain = req_host
        scheme = "https"
    else:
        domain = settings.domain
    redirect_uri = f"{scheme}://{domain}/auth/callback"
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })

    if token_resp.status_code != 200:
        # Log only status + error type — never the full response body.
        # Google's token endpoint can echo back partial credentials
        # (client_id fragments) or JWT prefixes in error details;
        # those must never reach application logs.
        try:
            err_type = token_resp.json().get("error", "unknown")
        except Exception:
            err_type = "non-json"
        logger.error(
            "Token exchange failed: status=%s error=%s",
            token_resp.status_code, err_type,
        )
        return RedirectResponse(url="/?error=token_exchange_failed")

    tokens = token_resp.json()
    access_token = tokens.get("access_token")

    # Get user info
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_resp.status_code != 200:
        # Same redaction rule as the token-exchange failure above —
        # log status + error type only, never the response body
        # (may contain access token echoes or PII on Google's side).
        try:
            err_type = userinfo_resp.json().get("error", "unknown")
        except Exception:
            err_type = "non-json"
        logger.error(
            "Userinfo fetch failed: status=%s error=%s",
            userinfo_resp.status_code, err_type,
        )
        return RedirectResponse(url="/?error=userinfo_failed")

    userinfo = userinfo_resp.json()
    email = userinfo.get("email", "").lower()
    hd = userinfo.get("hd", "")

    # Restrict to district domain
    if hd != settings.google_domain:
        logger.warning(f"Login denied — wrong domain: {email} (hd={hd})")
        return RedirectResponse(url="/?error=wrong_domain")

    # Enforce Google 2SV at login — opt-in via ENFORCE_2FA env var.
    # Off by default in the lite template; the receiving district
    # decides based on their own security policy. When on, fails closed
    # — SDK errors / missing creds / unenrolled all block login.
    if get_settings().enforce_2fa:
        from app.auth.group_sync import check_2fa_enrolled
        if not await check_2fa_enrolled(email):
            logger.warning(f"Login denied — 2FA not enrolled: {email}")
            return RedirectResponse(url="/?error=2fa_required")

    # Upsert user
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            email=email,
            name=userinfo.get("name"),
            picture=userinfo.get("picture"),
            last_login=datetime.now(timezone.utc),
        )
        db.add(user)
        await db.flush()  # Get user.id without committing — final commit after audit
        await db.refresh(user)
    else:
        user.name = userinfo.get("name")
        user.picture = userinfo.get("picture")
        user.last_login = datetime.now(timezone.utc)

    # Backfill inventory_pending_orders: when an admin confirms an
    # addressee via email before the addressee has ever logged in, the
    # order gets ship_to_email but assigned_user_id stays NULL. On this
    # user's first login we stitch those together so they can see their
    # orders immediately. Exact email match only — no fuzzy matching
    # (we already did that when admin confirmed).
    #
    # This UPDATE intentionally rides the same transaction as the login
    # upsert above — the user row and the assignment should commit or
    # roll back together. The outer `await db.commit()` later in this
    # handler commits both.
    try:
        from sqlalchemy import update as _upd
        from app.modules.inventory.models import InventoryPendingOrder
        stmt = (
            _upd(InventoryPendingOrder)
            .where(
                InventoryPendingOrder.assigned_user_id.is_(None),
                InventoryPendingOrder.ship_to_email == user.email,
            )
            .values(assigned_user_id=user.id)
        )
        res = await db.execute(stmt)
        if res.rowcount:
            logger.info(
                "Backfilled assigned_user_id for %d pending order(s) on login of %s",
                res.rowcount, email,
            )
    except Exception as e:
        logger.warning(f"pending-order assignee backfill failed for {email}: {e}")

    # Same pattern for tickets filed ON BEHALF OF this email before the
    # user had an account. Now that we have a users.id, stitch the FK
    # so their dashboard's visibility filter (which checks
    # on_behalf_of_user_id, not the raw email) surfaces the ticket.
    # Runs on every login — cheap idempotent UPDATE with no matching
    # rows on subsequent logins.
    try:
        from sqlalchemy import update as _upd
        from app.modules.tickets.models import Ticket
        stmt = (
            _upd(Ticket)
            .where(
                Ticket.on_behalf_of_user_id.is_(None),
                Ticket.on_behalf_of_email == user.email,
            )
            .values(on_behalf_of_user_id=user.id)
        )
        res = await db.execute(stmt)
        if res.rowcount:
            logger.info(
                "Linked %d on-behalf-of ticket(s) to newly-known user %s",
                res.rowcount, email,
            )
    except Exception as e:
        logger.warning(f"on-behalf-of ticket backfill failed for {email}: {e}")

    # Sync roles from Google Group membership
    assigned_roles = await sync_user_roles(db, user)

    if not assigned_roles:
        logger.warning(f"Login denied — no group membership: {email}")
        return RedirectResponse(url="/?error=no_access")

    # Fetch the user's Google Group memberships and store them in the
    # session. This is the single source of truth for cross-app
    # authorization — Fleet Watch (and any future sibling app that
    # reads Nexus sessions) can gate access via simple list membership
    # without needing its own Google Admin SDK credentials. The list
    # is refreshed on every login; group changes take effect at next
    # sign-in. Failure to fetch is non-fatal — the user still logs
    # into Nexus (role-based access still works via user_roles) but
    # sibling apps will see an empty list and deny by default.
    auth_groups: list[str] = []
    try:
        from app.integrations.google.adapter import GoogleWorkspaceAdapter
        google = GoogleWorkspaceAdapter(db)
        raw_groups = await google.get_user_groups(email)
        auth_groups = [
            (g.get("email") or "").strip().lower()
            for g in raw_groups
            if g.get("email")
        ]
        logger.info(f"Login: {email} — fetched {len(auth_groups)} groups for session")
    except Exception as ge:
        logger.warning(
            f"Login: {email} — group fetch failed ({ge}); "
            "sibling apps will see empty group list"
        )

    # Snapshot the user's role names into the session so sibling apps
    # (Fleet Watch, future ones) can apply Nexus's RBAC notion of
    # "admin" without re-querying the DB. `assigned_roles` was just
    # rebuilt by sync_user_roles() above, so it's authoritative.
    role_names = sorted({r.get("role_name") for r in assigned_roles if r.get("role_name")})

    # Rotate session — issue new session ID, delete pre-auth session
    # Prevents session fixation: attacker's known session ID is never reused
    from app.auth.session_store import rotate_session
    session_data = {
        "user_email": email,
        "user_id": user.id,
        "user_name": userinfo.get("name"),
        "user_picture": userinfo.get("picture"),
        "two_fa_enrolled": True,
        "two_fa_checked_at": int(time.time()),
        "auth_groups": auth_groups,
        "roles": role_names,
    }
    new_signed_cookie = await rotate_session(
        redis_client=request.scope["_session_redis"],
        old_signed_id=request.scope.get("_session_cookie_raw"),
        new_data=session_data,
        secret_key=request.scope["_session_secret"],
        max_age=request.scope["_session_max_age"],
    )
    # Signal the middleware to set the new cookie
    request.scope["_session_new_cookie"] = new_signed_cookie
    # Also update scope session so downstream code sees it
    request.scope["session"] = session_data

    # Audit login and commit the entire login flow
    # (user upsert + role sync + audit are all in this transaction)
    await log_action(
        db,
        actor=email,
        action="login",
        module="auth",
        details=f"roles: {[r['role_name'] for r in assigned_roles]}",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    logger.info(f"Login: {email} — {len(assigned_roles)} roles")

    # Resolve landing page from permissions
    from app.policies.engine import get_user_permissions
    permissions = await get_user_permissions(db, user.id)
    return await _resolve_landing(permissions)


@router.get("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    """Clear session and redirect to login."""
    email = request.session.get("user_email", "unknown")
    await log_action(
        db,
        actor=email,
        action="logout",
        module="auth",
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    request.session.clear()
    return RedirectResponse(url="/")


async def _resolve_landing(permissions: list[dict]) -> RedirectResponse:
    """Pick the best landing page based on the user's permissions."""
    actions = {p["action"] for p in permissions}

    # Core module users go to dashboard
    core_actions = {"dashboard.view", "staff.view", "network.view", "access.view"}
    if actions & core_actions:
        return RedirectResponse(url="/dashboard")

    # Ticket-only requesters (FMX-equivalent submitters) — must come
    # before the other narrower checks so they don't get routed to
    # /roster or /staff/intake just because they happen to hold one of
    # those permissions for a tangential reason.
    if "tickets.submit" in actions:
        return RedirectResponse(url="/tickets/submit")

    # Guidance-only
    if any(a.startswith("roster.guidance") for a in actions):
        return RedirectResponse(url="/guidance")

    # HR-only
    if "staff.request.submit" in actions and not (actions & core_actions):
        return RedirectResponse(url="/staff/intake")

    # Roster-only
    if any(a.startswith("roster.") for a in actions):
        return RedirectResponse(url="/roster")

    return RedirectResponse(url="/dashboard")
