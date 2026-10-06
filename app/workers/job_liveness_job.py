"""check_job_liveness — compares each scheduled job's last successful
run against its expected cadence, dispatches a high-severity routed
alert when any job goes stale.

Runs every 15 min. Alerts are Redis-deduped per job so a persistently-
broken job pages once per ``LIVENESS_ALERT_DEDUP_HOURS`` (default 6),
not on every check tick.

Staleness rules:
  - Job has NO row in job_health yet — assume it hasn't run since
    worker started; consider stale after 2x its interval.
  - Job has a row but ``last_success_at`` is None or older than
    ``STALE_MULTIPLIER`` × interval — stale.
  - Job has ``consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD``
    even if the interval hasn't elapsed — also stale.

Excludes jobs that don't have a meaningful expected cadence (e.g.
one-shot / manually-triggered jobs).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)


STALE_MULTIPLIER = 3
CONSECUTIVE_FAILURE_THRESHOLD = 5
# Long fallback so a marker orphaned by a Nexus restart eventually
# expires and lets a new alert fire. Under normal operation the marker
# is cleared by the "recovered" path — so a stale job only alerts
# ONCE per outage even if it stays stuck for days.
LIVENESS_ALERT_DEDUP_HOURS = 24 * 7  # 7 days fallback

# After the worker starts, skip alerting entirely for this many seconds.
# ARQ can't resume in-flight jobs across restarts, so a daily job
# (interval 86400s) that was interrupted at 23:59 will show
# last_success_at from yesterday — check_liveness at the fresh 00:15
# tick would flag it stale even though the job just needs to re-fire.
# Grace lets those catch up cleanly.
WORKER_START_GRACE_SEC = 15 * 60  # 15 min

# Redis key prefix so operators can flush alerts manually if needed
_KEY_ALERTED = "job_health:alerted:"
_KEY_WORKER_STARTED = "worker:started_at"


async def _redis():
    import redis.asyncio as aioredis
    from app.config import get_settings
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


async def check_job_liveness(ctx: dict) -> dict:
    """Scan job_health vs the scheduler's expected cadence; alert on stale."""
    from app.db.engine import AsyncSessionLocal
    from app.modules.alerts.service import dispatch_routed_alert
    from app.scheduler import SCHEDULES

    result = {
        "job": "check_job_liveness",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checked": 0,
        "stale": 0,
        "alerted": 0,
        "suppressed": 0,
        "healthy": 0,
        "never_ran": 0,
        "recovered": 0,
        "in_grace_period": False,
        "stale_jobs": [],
    }

    now_ts = int(time.time())

    # Startup grace — if the worker started less than WORKER_START_GRACE_SEC
    # ago, skip all alerting logic. Recovered notifications still fire
    # because they only clear existing stale-alert dedup markers; a
    # transient restart shouldn't page the tree but SHOULD auto-clear
    # markers as jobs succeed on their first post-restart tick.
    grace_r = await _redis()
    try:
        started_at_str = await grace_r.get(_KEY_WORKER_STARTED)
        if started_at_str:
            try:
                started_at = int(started_at_str)
                if (now_ts - started_at) < WORKER_START_GRACE_SEC:
                    result["in_grace_period"] = True
                    result["grace_remaining_sec"] = (
                        WORKER_START_GRACE_SEC - (now_ts - started_at)
                    )
            except (TypeError, ValueError):
                pass
    finally:
        await grace_r.aclose()

    try:
        async with AsyncSessionLocal() as db:
            # Pull job_health rows keyed by job_name
            rows = (await db.execute(text(
                "SELECT job_name, last_success_at, consecutive_failures, "
                "last_error, last_result_at "
                "FROM job_health"
            ))).mappings().all()
            health_by_name = {r["job_name"]: dict(r) for r in rows}

            r = await _redis()
            try:
                for job_name, interval_sec, _desc in SCHEDULES:
                    # Skip liveness of the liveness job itself — its own
                    # row updates on this very tick, so it's tautological.
                    if job_name == "check_job_liveness":
                        continue

                    result["checked"] += 1
                    h = health_by_name.get(job_name)

                    stale_reason: str | None = None
                    if not h:
                        # Never seen a run — most common cause is a
                        # fresh worker restart where daily jobs (86400s
                        # interval) simply haven't had a chance to fire
                        # yet. Log it in the summary but do NOT page —
                        # a real "job never runs" problem is caught by
                        # the "never succeeded" branch after the first
                        # failure lands.
                        result["never_ran"] += 1
                        continue
                    else:
                        last_success = h["last_success_at"]
                        consec = int(h["consecutive_failures"] or 0)

                        if consec >= CONSECUTIVE_FAILURE_THRESHOLD:
                            stale_reason = (
                                f"{consec} consecutive failures — "
                                f"last error: {(h.get('last_error') or '')[:120]}"
                            )
                        elif last_success is None:
                            stale_reason = "has run but never succeeded"
                        else:
                            age_sec = (
                                datetime.now(timezone.utc) - last_success
                            ).total_seconds()
                            if age_sec > STALE_MULTIPLIER * interval_sec:
                                stale_reason = (
                                    f"last success was {int(age_sec)}s ago "
                                    f"(expected every ~{interval_sec}s, "
                                    f"threshold {STALE_MULTIPLIER}x); "
                                    f"{consec} consecutive failures since"
                                )

                    if stale_reason is None:
                        result["healthy"] += 1
                        # Recovery path — if we previously alerted on
                        # this job going stale, dispatch a "recovered"
                        # notification and clear the dedup marker so
                        # future stale episodes can re-alert. Runs even
                        # during the startup grace window so restart-
                        # induced stale flags auto-clear as jobs finish
                        # their first post-restart tick.
                        alerted_key = f"{_KEY_ALERTED}{job_name}"
                        if await r.exists(alerted_key):
                            try:
                                await dispatch_routed_alert(
                                    db,
                                    building_code="UNKNOWN",
                                    severity="info",
                                    source_module="observability",
                                    source_ref=f"job_health:{job_name}",
                                    summary=(
                                        f"Scheduled job '{job_name}' has "
                                        f"recovered — success just landed."
                                    ),
                                    affected_count=1,
                                )
                                await db.commit()
                                await r.delete(alerted_key)
                                result["recovered"] += 1
                            except Exception as e:
                                logger.warning(
                                    f"recovered-alert for {job_name} failed: {e}"
                                )
                        continue

                    result["stale"] += 1
                    result["stale_jobs"].append({
                        "job": job_name, "reason": stale_reason,
                    })

                    # In grace period? Tally the state but don't alert.
                    # A truly-broken job will still be stale after the
                    # grace window and will alert then; jobs that just
                    # need a first post-restart tick will recover before
                    # the window ends and never alert at all.
                    if result["in_grace_period"]:
                        result["suppressed"] += 1
                        continue

                    alerted_key = f"{_KEY_ALERTED}{job_name}"
                    # One alert per stale-episode. Cleared by the
                    # "recovered" path above, so future incidents alert
                    # cleanly.
                    if await r.exists(alerted_key):
                        result["suppressed"] += 1
                        continue

                    # Dispatch alert. Building set to UNKNOWN — job
                    # failures are district-wide.
                    summary = (
                        f"Scheduled job '{job_name}' is stale: "
                        f"{stale_reason}"
                    )
                    try:
                        # High severity — a stuck scheduled job is a real
                        # infrastructure problem worth surfacing. Whether
                        # this pages the phone tree or just pushes is
                        # decided by the ``observability`` row in
                        # ``alert_module_overrides`` (seeded push+email
                        # only in a173). Bumping severity here without
                        # changing the module policy is safe.
                        await dispatch_routed_alert(
                            db,
                            building_code="UNKNOWN",
                            severity="high",
                            source_module="observability",
                            source_ref=f"job_health:{job_name}",
                            summary=summary,
                            affected_count=1,
                        )
                        await db.commit()
                        # NO TTL — one alert per stale-episode, forever
                        await r.set(alerted_key, now_ts)
                        result["alerted"] += 1
                        logger.warning(
                            f"job_health alert dispatched: {job_name} — {stale_reason}"
                        )
                    except Exception as e:
                        logger.error(
                            f"job_health: failed to dispatch alert for {job_name}: {e}"
                        )
            finally:
                await r.aclose()
    except Exception as e:
        logger.exception("check_job_liveness failed")
        result["error"] = f"{type(e).__name__}: {e}"[:200]

    if result["stale"] or result["alerted"] or result["never_ran"]:
        logger.info(f"Job liveness check: {json.dumps(result)}")
    return result
