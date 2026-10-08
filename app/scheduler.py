"""
Command Nexus v2 — Scheduler entrypoint.

Enqueues recurring jobs only. Does NOT run business logic directly.
The worker process executes the actual jobs.

Run with: python -m app.scheduler
"""

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Schedule definitions: (job_name, interval_seconds, description)
SCHEDULES = [
    ("health_check_job", 300, "Worker health check every 5 minutes"),
    ("sync_staff_directory", 43200, "Google staff directory sync 2x daily"),
    ("sync_hr_data", 43200, "HR sheet data cache sync 2x daily"),
    ("sync_paxton_photos", 86400, "Staff photo sync once daily"),
    ("check_hr_diffs", 900, "HR change diff every 15 min — queues onboard/offboard tasks"),
    ("expire_stale_onboarding_tokens", 3600, "Expire onboarding tokens past their TTL — hourly"),
    ("run_staff_reconciliation", 3600, "Rebuild staff_reconciliation from staff_directory + HR — hourly"),
    ("check_job_liveness", 900, "Alert if any scheduled job goes stale — every 15 min"),
    ("poll_clever_imports", 900, "Poll Gmail for MetaSolutions CSV exports (roster) — every 15 min"),
    ("reconcile_student_google_state", 86400, "Reconcile Google student OU membership vs. roster — daily"),
]



async def run_scheduler():
    """Main scheduler loop — enqueues recurring jobs on their schedules."""
    from app.audit.logging_config import configure_logging
    configure_logging()

    from app.config import get_settings
    from arq import create_pool
    from arq.connections import RedisSettings

    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    logger.info(f"Scheduler started — {len(SCHEDULES)} job(s) configured")
    for name, interval, desc in SCHEDULES:
        logger.info(f"  {name}: every {interval}s — {desc}")

    # Track last enqueue time per job
    last_enqueue: dict[str, float] = {}

    # Heartbeat file — docker HEALTHCHECK reads this file's mtime,
    # autoheal restarts the container if the loop hangs.
    import pathlib
    heartbeat_path = pathlib.Path("/tmp/nexus_scheduler.heartbeat")

    while True:
        try:
            heartbeat_path.touch(exist_ok=True)
        except Exception:
            pass

        now = datetime.now(timezone.utc).timestamp()

        for job_name, interval, _ in SCHEDULES:
            last = last_enqueue.get(job_name, 0)
            if now - last >= interval:
                try:
                    # Job ID must be unique per intended execution window.
                    # Divide epoch by interval so each window gets its own ID.
                    # e.g. 900s interval → new ID every 15 minutes
                    window = int(datetime.now(timezone.utc).timestamp() // interval)
                    await redis.enqueue_job(job_name, _job_id=f"scheduled:{job_name}:{window}")
                    last_enqueue[job_name] = now
                    logger.debug(f"Enqueued {job_name}")
                except Exception as e:
                    logger.error(f"Failed to enqueue {job_name}: {e}")

        await asyncio.sleep(30)  # Check every 30 seconds


if __name__ == "__main__":
    asyncio.run(run_scheduler())
