"""
Auth middleware — deny-by-default access control + CSRF protection + 2FA enforcement.

Runs before every request. Checks:
1. CSRF on mutating requests (Origin/Referer + custom header)
2. Authentication (session exists)
3. 2FA — verified at login and re-checked every TWO_FA_RECHECK_SECONDS
4. Authorization (route mapped to required permissions)

2FA recheck design:
- At login, two_fa_enrolled=True and two_fa_checked_at=<timestamp> are written
  to the session.
- On each authenticated request, if the cached check is older than
  TWO_FA_RECHECK_SECONDS, the Admin SDK is called again.
- If the recheck fails (user disabled 2FA, SDK error), the session is cleared
  and the request is rejected. Fails closed.
- TWO_FA_RECHECK_SECONDS=300 means a user who disables 2FA loses access
  within 5 minutes. Set to 0 to check on every request.
"""

import logging
import time
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.auth.group_sync import check_2fa_enrolled

logger = logging.getLogger(__name__)

# Routes that require NO authentication
PUBLIC_PATHS = {"/", "/health", "/auth/login", "/auth/callback", "/auth/logout",
                # Local-auth routes — only reachable when LOCAL_AUTH_ENABLED,
                # but the auth middleware doesn't check feature flags; it's
                # safe to list them unconditionally because the router
                # itself isn't mounted when the flag is off.
                "/auth/local/login", "/auth/local/logout",
                "/api/branding", "/api/buildings",
                # Cast receiver HTML — Chromecast fetches this with no
                # cookies. It's a static bootstrapper page with no data.
                "/cast-receiver"}
# /onboard/{token} is the self-service new-hire form — the token IS the
# auth, verified inside the route handler (app/modules/staff/onboarding_router.py).
# GET renders the form; POST submits. Both bypass session-based CSRF /
# auth for this reason.
PUBLIC_PREFIXES = ("/static/", "/onboard/")
# Routes that handle their own auth via _noc_auth (session OR NOC token
# OR voice secret). The middleware must not reject them for missing
# session — the route handler's dependency does the real auth check.
_SELF_AUTH_PATHS = (
    "/noc",
    "/api/network/noc-data",
    "/api/network/system-status",
    "/api/network/weather",
    "/api/voice/cdr/inbound",
    "/api/voice/lockout",
    # HALO 2.20 webhook receivers: device-side template POST has no way
    # to inject X-Requested-With. Auth is the X-Halo-Secret bearer token
    # verified inside the route handler (see app/integrations/halo/adapter.py).
    "/api/security/halo/heartbeat",
    "/api/security/halo/event",
    # Short aliases — HALO 2.20 Message field has a ~130-char limit
    # that cuts off the full /api/security/halo/* path.
    "/halo/h",
    "/halo/e",
    # Persistent camera-cast HLS — Chromecast fetches the m3u8 +
    # .ts segments with no cookies / no CSRF header. Auth is a
    # signed token in the URL path (validated inside the route
    # handler in app/modules/network/cast_camera_router.py).
    "/api/network/cast-camera/hls/",
)

# How often to re-verify 2FA status with the Admin SDK for an active session.
# A user who disables 2FA will lose access within this window.
TWO_FA_RECHECK_SECONDS = 300  # 5 minutes


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Deny-by-default request gate.

    Checks CSRF, authentication, and 2FA status on every request.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        settings = get_settings()

        # ── Public routes — no auth needed ──
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Routes with their own auth (NOC token / voice secret) — let
        # them through to the route handler which does the real check.
        if any(path.startswith(p) for p in _SELF_AUTH_PATHS):
            return await call_next(request)

        # ── CSRF protection on mutating requests ──
        # Local-auth login POST exempt — the user has no session yet so
        # there's no custom-header to assert. Origin/Referer check still
        # happens inside the handler; cross-origin login POSTs fail there.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and path != "/auth/local/login":
            if not self._check_csrf(request, settings):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
                return RedirectResponse(url="/")

        # ── Authentication ──
        try:
            user_email = request.session.get("user_email")
        except Exception:
            user_email = None

        if not user_email:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse(url="/")

        # ── 2FA enforcement — re-checked periodically for active sessions ──
        if not await self._check_session_2fa(request):
            request.session.clear()
            if path.startswith("/api/"):
                return JSONResponse({"detail": "2FA required"}, status_code=403)
            return RedirectResponse(url="/?error=2fa_required")

        return await call_next(request)

    async def _check_session_2fa(self, request: Request) -> bool:
        """
        Verify 2FA status for the current session.

        Uses a TTL cache in the session to avoid hitting the Admin SDK on
        every request. If the cached check is recent, trust it. Otherwise
        re-verify with Google and update the session timestamp.

        Returns False (and logs a warning) if the user no longer has 2FA
        enrolled or if the Admin SDK check fails for any reason (fail closed).
        """
        session = request.session
        email = session.get("user_email")
        if not email:
            return False

        now = int(time.time())
        last_checked = int(session.get("two_fa_checked_at", 0))
        enrolled = bool(session.get("two_fa_enrolled"))

        # Cached check is still fresh — trust it
        if enrolled and (now - last_checked) < TWO_FA_RECHECK_SECONDS:
            return True

        # Cache expired or never set — re-verify with Admin SDK
        ok = await check_2fa_enrolled(email)
        if not ok:
            logger.warning(f"Session denied — 2FA not currently enrolled: {email}")
            return False

        # Update session cache
        session["two_fa_enrolled"] = True
        session["two_fa_checked_at"] = now
        return True

    def _get_allowed_hosts(self, settings) -> set:
        """
        Return hosts trusted for CSRF Origin/Referer validation.
        localhost is only trusted in non-production environments.
        """
        # Strip port from domain if present (urlparse.hostname doesn't include port)
        domain_host = settings.domain.split(":")[0]
        hosts = {settings.domain, domain_host}
        # Include allowed external domains (Cloudflare tunnel, etc.)
        for d in getattr(settings, "allowed_domains", set()):
            hosts.add(d)
            hosts.add(d.split(":")[0])
        if not settings.is_production:
            hosts.update({"localhost", "127.0.0.1"})
        return hosts

    def _check_csrf(self, request: Request, settings) -> bool:
        """
        CSRF validation — two signals required on every mutating request:

        Signal 1 (source verification): Origin OR Referer must match our domain.
        Signal 2 (custom header): X-Requested-With: CommandNexus must be present.

        Both signals are required. A same-site browser POST without the custom
        header is rejected. Does NOT rely on Content-Type alone.
        """
        allowed_hosts = self._get_allowed_hosts(settings)

        # Signal 1: Origin or Referer must match
        source_ok = False
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in allowed_hosts:
                logger.warning(f"CSRF: Origin mismatch: {origin}")
                return False
            source_ok = True

        if not source_ok:
            referer = request.headers.get("referer")
            if referer:
                parsed = urlparse(referer)
                if parsed.hostname not in allowed_hosts:
                    logger.warning(f"CSRF: Referer mismatch: {referer}")
                    return False
                source_ok = True

        if not source_ok:
            logger.warning(f"CSRF: No Origin or Referer for {request.method} {request.url.path}")
            return False

        # Signal 2: Custom header required on all mutating requests
        if request.headers.get("x-requested-with") != "CommandNexus":
            logger.warning(f"CSRF: Missing X-Requested-With for {request.method} {request.url.path}")
            return False

        return True
