"""
Command Nexus (lite) — API entrypoint.

Trimmed distribution built for districts that want the staff + student
management workflow without the full the district-specific integration surface
(Paxton, HP ProCurve, Grandstream UCM, HALO, CareHawk, Wave, etc.).

Prereqs a district brings: Google Workspace + a SIS that can email CSV
exports (PowerSchool/MetaSolutions is the reference implementation).
Everything else configured through Settings after first-run.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    from app.audit.logging_config import configure_logging
    configure_logging()

    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=100))

    from app.db.engine import init_db
    await init_db()

    logger.info("Command Nexus (lite) started")

    # Pre-load branding cache so the very first page request already has
    # district name + color — avoids the "NEXUS → <DISTRICT> NEXUS" flash
    # because the middleware can now put the real values on request.state
    # before any template renders.
    try:
        from app.db.engine import AsyncSessionLocal
        from app import branding
        async with AsyncSessionLocal() as db:
            await branding.refresh(db)
    except Exception as e:
        logger.warning("Branding preload failed, using defaults: %s", e)

    # Local-auth bootstrap — two paths:
    #   1. If secrets/bootstrap_local_admin exists (first_run.sh wrote it),
    #      apply it + remove the file.
    #   2. Else if the feature flag is on but local_users is empty, create
    #      a default admin (admin@local / changeme123!) with
    #      must_change_password=true so the first login forces a reset.
    try:
        if get_settings().local_auth_enabled:
            import os as _os
            from app.db.engine import AsyncSessionLocal
            from sqlalchemy import text as _text
            boot_path = _os.environ.get("SECRETS_DIR", "/run/secrets") + "/bootstrap_local_admin"
            applied = False
            async with AsyncSessionLocal() as _db:
                if _os.path.isfile(boot_path):
                    with open(boot_path) as _f:
                        _lines = _f.read().strip().splitlines()
                    if len(_lines) == 2:
                        _email, _pw = _lines[0].strip().lower(), _lines[1]
                        from app.auth.local import _hasher
                        await _db.execute(_text("""
                            INSERT INTO local_users (email, display_name, pw_hash, is_admin, created_by, must_change_password)
                            VALUES (:e, :e, :p, true, 'system:bootstrap', false)
                            ON CONFLICT (email) DO UPDATE SET
                                pw_hash = EXCLUDED.pw_hash, is_admin = true, active = true,
                                must_change_password = false
                        """).bindparams(e=_email, p=_hasher().hash(_pw)))
                        await _db.commit()
                        logger.info("Local-auth bootstrap: admin %s seeded from secrets file", _email)
                        applied = True
                        try: _os.remove(boot_path)
                        except OSError: pass
                if not applied:
                    # Create default admin if the table is empty
                    row = (await _db.execute(_text(
                        "SELECT COUNT(*) FROM local_users"
                    ))).scalar_one()
                    if row == 0:
                        from app.auth.local import _hasher
                        await _db.execute(_text("""
                            INSERT INTO local_users (email, display_name, pw_hash, is_admin, created_by, must_change_password)
                            VALUES ('admin@local', 'Default Admin', :p, true, 'system:default', true)
                        """).bindparams(p=_hasher().hash('changeme123!')))
                        await _db.commit()
                        logger.warning(
                            "Local-auth: default admin created — email=admin@local "
                            "password=changeme123! — MUST change on first login"
                        )
    except Exception as e:
        logger.warning("Local-auth bootstrap failed (non-fatal): %s", e)

    # Worker watchdog — pages when the worker container goes silent.
    # Leader-locked via fcntl so only one uvicorn worker actually runs it.
    from app.observability import worker_watchdog as _wd
    _wd_task, _wd_lock = _wd.start()
    app.state._wd_task = _wd_task
    app.state._wd_lock = _wd_lock

    yield

    # Stop worker watchdog (only the leader has the task).
    try:
        from app.observability import worker_watchdog as _wd
        await _wd.stop(
            getattr(app.state, "_wd_task", None),
            getattr(app.state, "_wd_lock", None),
        )
    except Exception:
        pass
    logger.info("Command Nexus (lite) shutting down")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Command Nexus",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # Middleware — Starlette processes in REVERSE add order.
    # Execution order: Correlation → RedisSession → Auth → Branding →
    # CSPNonce → route handler.
    from app.security.csp import CSPNonceMiddleware
    app.add_middleware(CSPNonceMiddleware)

    from app.branding.middleware import BrandingMiddleware
    app.add_middleware(BrandingMiddleware)

    from app.auth.middleware import AuthMiddleware
    app.add_middleware(AuthMiddleware)

    from app.auth.session_store import RedisSessionMiddleware
    app.add_middleware(RedisSessionMiddleware)

    from app.audit.middleware import CorrelationMiddleware
    app.add_middleware(CorrelationMiddleware)  # LAST = outermost

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index(request: Request):
        if request.session.get("user_email"):
            return RedirectResponse(url="/dashboard")
        templates = Jinja2Templates(directory="app/templates")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "local_auth_enabled": settings.local_auth_enabled,
            "google_auth_enabled": settings.google_auth_enabled,
        })

    _register_routers(app)
    return app


def _register_routers(app: FastAPI):
    """Import and mount all module routers."""
    # Google OAuth routes mounted only when GOOGLE_AUTH_ENABLED. Keeps
    # the attack surface zero for districts that don't use Google SSO.
    if get_settings().google_auth_enabled:
        from app.auth.oauth import router as auth_router
        app.include_router(auth_router)

    from app.modules.audit.router import router as audit_router
    app.include_router(audit_router)

    from app.workers.router import router as jobs_router
    app.include_router(jobs_router)

    from app.modules.settings.router import router as settings_router
    app.include_router(settings_router)

    from app.modules.dashboard.router import router as dashboard_router
    app.include_router(dashboard_router)

    from app.modules.staff.router import router as staff_router
    app.include_router(staff_router)

    from app.modules.staff.onboarding_router import router as onboarding_router
    app.include_router(onboarding_router)

    from app.modules.staff.exports_router import router as staff_exports_router
    app.include_router(staff_exports_router)

    from app.modules.roster.router import router as roster_router
    app.include_router(roster_router)

    from app.modules.roster.exports_router import router as roster_exports_router
    app.include_router(roster_exports_router)

    from app.modules.roster.analytics_router import router as roster_analytics_router
    app.include_router(roster_analytics_router)

    from app.modules.roster.custom_sections_router import router as custom_sections_router
    app.include_router(custom_sections_router)

    # alerts UI CRUD stripped in the lite build — dispatch still lives
    # in app.modules.alerts.service (called from worker_watchdog etc.).

    # Local-auth routes — only mounted when the feature flag is on so
    # districts that use SSO-only have zero attack surface here.
    if get_settings().local_auth_enabled:
        from app.auth.local import router as local_auth_router, admin_router as local_users_admin_router
        app.include_router(local_auth_router)
        app.include_router(local_users_admin_router)

    # /me self-service page stripped in the lite build (was tied to
    # VoiceRecipient bindings that we removed). Add back as a proper
    # push-only preferences page in a follow-up.


app = create_app()
