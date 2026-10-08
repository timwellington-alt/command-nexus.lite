"""
Command Nexus (lite) — Worker entrypoint.

Executes background jobs: provisioning workflows, polling, notifications.
Uses ARQ with Redis as the job queue.

Run with: arq app.worker.WorkerSettings
"""

import logging

from app.workers.health_job import health_check_job
from app.workers.staff_sync_job import sync_staff_directory
from app.workers.reconcile_students_job import reconcile_student_google_state
from app.workers.photo_sync_job import sync_paxton_photos
from app.workers.hr_diff_job import check_hr_diffs
from app.workers.hr_sync_job import sync_hr_data
from app.workers.onboarding_token_expire_job import expire_stale_onboarding_tokens
from app.workers.job_liveness_job import check_job_liveness
from app.workers.staff_reconciliation_job import run_staff_reconciliation
from app.workers.clever_import_job import poll_clever_imports

from arq import func as _arq_func


def _track_job(job_func):
    """Wrap a scheduled job so its success/failure gets recorded in
    ``job_health`` — feeds ``check_job_liveness`` alerting.

    Rethrows the original exception so ARQ still sees the failure and
    retries per its own policy. Recorder failures are swallowed inside
    the recorder itself so observability can never cascade an outage.
    """
    from functools import wraps
    import time as _time

    @wraps(job_func)
    async def _wrapped(*args, **kwargs):
        from app.services.job_health import record_job_success, record_job_failure
        started = _time.monotonic()
        try:
            result = await job_func(*args, **kwargs)
            await record_job_success(job_func.__name__, _time.monotonic() - started)
            return result
        except Exception as e:
            await record_job_failure(
                job_func.__name__,
                f"{type(e).__name__}: {str(e)[:400]}",
                _time.monotonic() - started,
            )
            raise
    return _wrapped


# Roster import + provisioning walks every diff'd student and hits
# Google's set_student_id per user. With retries it can eat minutes per
# student; a 30-min timeout keeps end-of-year rollovers alive.
poll_clever_imports_job = _arq_func(
    _track_job(poll_clever_imports),
    name="poll_clever_imports",
    timeout=1800,
    max_tries=1,
)

# Reconcile walks every student OU in Google with per-user detail pulls.
reconcile_student_google_state_job = _arq_func(
    _track_job(reconcile_student_google_state),
    name="reconcile_student_google_state",
    timeout=1800,
    max_tries=1,
)


logger = logging.getLogger(__name__)


async def startup(ctx: dict):
    """Worker startup — initialize DB and logging."""
    from app.audit.logging_config import configure_logging
    configure_logging()

    from app.db.engine import init_db
    await init_db()

    # Stamp worker start time in Redis so check_job_liveness can honor
    # a startup grace period.
    try:
        import time as _time
        import redis.asyncio as _aioredis
        from app.config import get_settings as _get_settings
        _r = _aioredis.from_url(_get_settings().redis_url, decode_responses=True)
        await _r.set("worker:started_at", int(_time.time()))
        await _r.aclose()
    except Exception as e:
        logger.warning("failed to stamp worker:started_at in redis: %s", e)

    # Heartbeat file — docker HEALTHCHECK reads this file's mtime.
    import asyncio as _asyncio
    async def _heartbeat_loop():
        import pathlib
        p = pathlib.Path("/tmp/nexus_worker.heartbeat")
        while True:
            try:
                p.touch(exist_ok=True)
            except Exception:
                pass
            try:
                await _asyncio.sleep(30)
            except _asyncio.CancelledError:
                return
    ctx["_heartbeat_task"] = _asyncio.create_task(_heartbeat_loop(), name="heartbeat")

    logger.info("Worker started")


async def shutdown(ctx: dict):
    """Worker shutdown — cleanup."""
    hb = ctx.get("_heartbeat_task")
    if hb is not None:
        hb.cancel()
        try:
            await hb
        except Exception:
            pass
    logger.info("Worker shutting down")


class WorkerSettings:
    """ARQ worker configuration."""

    functions = [_track_job(f) for f in [
        health_check_job,
        sync_staff_directory,
        sync_paxton_photos,
        check_hr_diffs,
        sync_hr_data,
        run_staff_reconciliation,
        expire_stale_onboarding_tokens,
        check_job_liveness,
    ]] + [
        poll_clever_imports_job,
        reconcile_student_google_state_job,
    ]

    on_startup = startup
    on_shutdown = shutdown

    keep_result = 86400
    keep_result_forever = False
    max_jobs = 10
    job_timeout = 600
