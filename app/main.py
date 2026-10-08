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

    # Local-auth bootstrap moved out of lifespan — see
    # app/scripts/seed_local_admin.py. first_run.sh runs that script
    # explicitly after `alembic upgrade head` so any failure is visible
    # (previously the lifespan version silently swallowed exceptions,
    # leaving operators with an empty local_users table + no clue why).
    #
    # Operator intervention path: `docker compose exec api
    #   python -m app.scripts.seed_local_admin`
    if get_settings().local_auth_enabled:
        try:
            from app.db.engine import AsyncSessionLocal
            from sqlalchemy import text as _text
            async with AsyncSessionLocal() as _db:
                _count = (await _db.execute(
                    _text("SELECT COUNT(*) FROM local_users WHERE active"),
                )).scalar_one()
            if _count == 0:
                logger.warning(
                    "Local-auth is enabled but local_users is empty. "
                    "Run: docker compose exec api python -m app.scripts.seed_local_admin"
                )
            else:
                logger.info("Local-auth: %s active account(s) on file", _count)
        except Exception as e:
            logger.warning("Local-auth presence check failed: %s", e)

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

    # Attendance analytics + Clever custom sections routers stripped
    # in lite — those features (per-student absence tracking, chronic
    # flag, intervention sheet sync) are district-specific and not
    # part of the shareable scope.
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
