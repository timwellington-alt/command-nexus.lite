"""Per-job liveness recorder.

Called from an ARQ ``on_job_end`` hook (see ``app/worker.py``) on every
job completion — success or failure. Writes into ``job_health`` and
appends a per-run row to ``job_runs`` for drill-in on /operations/health.

Kept tiny + defensive so the recorder itself can't cascade an outage:
- Every write happens in its own AsyncSession and swallows exceptions
  (log-only). Failing to record a job's outcome must not kill the job.
- Never imports ORM models; uses raw SQL so it can't be broken by the
  same mapper-init class of bug it exists to detect.
- Per-run history is capped to KEEP_RUNS_PER_JOB newest rows per job
  so the table can't balloon on a chatty short-cadence job.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)


KEEP_RUNS_PER_JOB = 100


async def _append_run(db, job_name: str, ended_at, ok: bool,
                      duration_sec: float | None, error: str | None) -> None:
    """Insert one row into job_runs and prune older rows past the cap.
    Called inside the same session as the job_health upsert."""
    err_clipped = (error or "")[:2000] if error else None
    started_at = ended_at  # duration is separate; ended_at is when we finished
    await db.execute(text("""
        INSERT INTO job_runs (job_name, started_at, ended_at, ok, duration_sec, error)
        VALUES (:n, :s, :e, :ok, :dur, :err)
    """).bindparams(n=job_name, s=started_at, e=ended_at, ok=ok,
                    dur=duration_sec, err=err_clipped))
    # Trim to newest KEEP_RUNS_PER_JOB per job. Runs on every insert but
    # only deletes the row that just fell off the tail (usually 0 rows).
    await db.execute(text("""
        DELETE FROM job_runs
        WHERE job_name = :n
          AND id NOT IN (
              SELECT id FROM job_runs
              WHERE job_name = :n
              ORDER BY id DESC
              LIMIT :k
          )
    """).bindparams(n=job_name, k=KEEP_RUNS_PER_JOB))


async def record_job_success(job_name: str, duration_sec: float | None = None) -> None:
    """Stamp a successful run for ``job_name``. Resets consecutive_failures.
    Appends a row to job_runs for drill-in."""
    try:
        from app.db.engine import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            await db.execute(text("""
                INSERT INTO job_health (
                    job_name, last_success_at, last_result_at,
                    consecutive_failures, last_error,
                    last_duration_sec, total_success, total_failure
                )
                VALUES (:n, :now, :now, 0, NULL, :dur, 1, 0)
                ON CONFLICT (job_name) DO UPDATE SET
                    last_success_at = EXCLUDED.last_success_at,
                    last_result_at = EXCLUDED.last_result_at,
                    consecutive_failures = 0,
                    last_error = NULL,
                    last_duration_sec = EXCLUDED.last_duration_sec,
                    total_success = job_health.total_success + 1
            """).bindparams(n=job_name, now=now, dur=duration_sec))
            await _append_run(db, job_name, now, True, duration_sec, None)
            await db.commit()
    except Exception as e:
        # Recorder failures MUST NOT propagate — the whole point of this
        # module is observability, and losing an observation is
        # infinitely better than crashing the job we're observing.
        logger.warning(f"job_health.record_success({job_name}) failed: {e}")


async def record_job_failure(
    job_name: str,
    error: str,
    duration_sec: float | None = None,
) -> None:
    """Stamp a failed run for ``job_name``. Increments consecutive_failures.
    Appends a row to job_runs for drill-in."""
    try:
        from app.db.engine import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            err = (error or "")[:2000]  # cap so a giant traceback doesn't bloat rows
            await db.execute(text("""
                INSERT INTO job_health (
                    job_name, last_failure_at, last_result_at,
                    consecutive_failures, last_error,
                    last_duration_sec, total_success, total_failure
                )
                VALUES (:n, :now, :now, 1, :err, :dur, 0, 1)
                ON CONFLICT (job_name) DO UPDATE SET
                    last_failure_at = EXCLUDED.last_failure_at,
                    last_result_at = EXCLUDED.last_result_at,
                    consecutive_failures = job_health.consecutive_failures + 1,
                    last_error = EXCLUDED.last_error,
                    last_duration_sec = EXCLUDED.last_duration_sec,
                    total_failure = job_health.total_failure + 1
            """).bindparams(n=job_name, now=now, err=err, dur=duration_sec))
            await _append_run(db, job_name, now, False, duration_sec, err)
            await db.commit()
    except Exception as e:
        logger.warning(f"job_health.record_failure({job_name}) failed: {e}")
