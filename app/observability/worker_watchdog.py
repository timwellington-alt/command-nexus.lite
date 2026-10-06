"""API-side worker watchdog.

The in-worker check_job_liveness monitor can't detect the case where
the entire worker container is dead (2026-09-17 outage: pychromecast
FD leak → worker unhealthy → no jobs ran for 4 days → nobody knew).
This module runs INSIDE the API container, which almost never goes
down, and pages when it sees no job activity at all.

Signal: `MAX(started_at) FROM job_runs`. If that timestamp is older
than `STALE_THRESHOLD_SEC` (default 30 min), the worker is dead.
Redis-deduped per-episode so a persistent outage pages once, not
every 5 min. Recovery clears the marker.

Leader-locked with fcntl (same pattern as the Blitzortung subscriber)
so a 4-uvicorn-worker API container only runs one watchdog instance.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)


# How often the watchdog checks job_runs. Cheap query so tight cadence
# is fine; keeping it 5 min so a transient DB blip doesn't false-alert.
POLL_INTERVAL_SEC = 5 * 60

# Threshold to declare the worker dead. The worker fires ~90 different
# scheduled jobs; the least frequent runs every 15 min. Anything past
# 30 min of NO job_runs means something is very wrong. Configurable
# for future tuning.
STALE_THRESHOLD_SEC = 30 * 60

# On first startup, wait this long before running the first check —
# gives the worker time to boot + fire its first job. Otherwise a
# concurrent API+worker restart false-alerts on the first tick.
STARTUP_GRACE_SEC = 5 * 60

# Redis dedup so a persistent outage alerts once per episode.
_KEY_ALERTED = "worker_watchdog:alerted"
_LOCK_PATH = "/tmp/worker_watchdog.leader.lock"


async def _redis():
    import redis.asyncio as aioredis
    from app.config import get_settings
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def _check_once() -> dict:
    """One tick — read newest job_runs.started_at, alert if stale."""
    from app.db.engine import AsyncSessionLocal
    from app.modules.alerts.service import dispatch_routed_alert

    result = {"stale": False, "alerted": False, "recovered": False}
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text(
            "SELECT MAX(started_at) AS latest FROM job_runs"
        ))).mappings().first()
        latest = row["latest"] if row else None
        if latest is None:
            # No job_runs at all — could be a brand-new DB or the
            # worker literally never ran. Don't alert; the worker's
            # own liveness monitor will handle its "never ran" state
            # once it comes up.
            return result

        age_sec = (datetime.now(timezone.utc) - latest).total_seconds()
        result["latest"] = latest.isoformat()
        result["age_sec"] = int(age_sec)

        r = await _redis()
        try:
            if age_sec > STALE_THRESHOLD_SEC:
                result["stale"] = True
                if await r.exists(_KEY_ALERTED):
                    return result  # already alerted this episode
                try:
                    await dispatch_routed_alert(
                        db,
                        building_code="UNKNOWN",
                        severity="high",
                        source_module="observability",
                        source_ref="worker_watchdog",
                        summary=(
                            f"Nexus worker appears DEAD — no job has run in "
                            f"{int(age_sec / 60)} minutes (last: "
                            f"{latest.strftime('%Y-%m-%d %H:%M UTC')}). "
                            f"Check nexus-v2-worker + nexus-v2-scheduler."
                        ),
                        affected_count=1,
                    )
                    await db.commit()
                    await r.set(_KEY_ALERTED, int(time.time()))
                    result["alerted"] = True
                    logger.warning(
                        "worker_watchdog: alert dispatched — last run %ds ago",
                        int(age_sec),
                    )
                except Exception as e:
                    logger.exception("worker_watchdog dispatch failed: %s", e)
            else:
                # Recovery — if we previously alerted and jobs are
                # flowing again, dispatch an "info" recovery notice
                # and clear the dedup marker so a future outage can
                # re-alert cleanly.
                if await r.exists(_KEY_ALERTED):
                    try:
                        await dispatch_routed_alert(
                            db,
                            building_code="UNKNOWN",
                            severity="info",
                            source_module="observability",
                            source_ref="worker_watchdog",
                            summary=(
                                "Nexus worker recovered — jobs are running "
                                f"again (last: {latest.strftime('%H:%M UTC')})."
                            ),
                            affected_count=1,
                        )
                        await db.commit()
                        await r.delete(_KEY_ALERTED)
                        result["recovered"] = True
                        logger.info("worker_watchdog: recovery alert dispatched")
                    except Exception as e:
                        logger.warning("worker_watchdog recovery dispatch failed: %s", e)
        finally:
            await r.aclose()
    return result


async def _watchdog_loop():
    """Long-running task — poll every POLL_INTERVAL_SEC forever."""
    await asyncio.sleep(STARTUP_GRACE_SEC)
    while True:
        try:
            await _check_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker_watchdog check failed")
        try:
            await asyncio.sleep(POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise


def start() -> tuple[asyncio.Task | None, "os._TemporaryFileWrapper | None"]:
    """Called from lifespan on API startup. Returns (task, lock_fh)
    so lifespan shutdown can cancel + release cleanly. Only one
    uvicorn worker acquires the flock; the rest skip.

    Returns (None, None) if another worker is already leader — the
    caller doesn't need to do anything with those.
    """
    try:
        fh = open(_LOCK_PATH, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
    except (IOError, OSError):
        logger.info("worker_watchdog: another worker holds the leader lock, skipping")
        return None, None

    logger.info("worker_watchdog: acquired leader lock (pid=%d), starting", os.getpid())
    task = asyncio.create_task(_watchdog_loop(), name="worker_watchdog")
    return task, fh


async def stop(task: asyncio.Task | None, fh) -> None:
    """Called from lifespan on API shutdown. Cancels the task and
    releases the leader lock."""
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    if fh is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except Exception:
            pass
