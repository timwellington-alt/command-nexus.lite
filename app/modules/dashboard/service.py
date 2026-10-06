"""
Dashboard summary service — cross-module aggregation.

This is the ONLY file that queries across modules for dashboard data.
Never called from anywhere except the dashboard router.

Rules:
- Never returns student PII (per T0.4)
- Each function returns {status: "ok"|"degraded"|"unavailable", ...}
- Exceptions never propagate — caller gets "unavailable" on any error
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog

logger = logging.getLogger(__name__)

CACHE_KEY = "dashboard:integration_status"
CACHE_TTL = 300  # 5 minutes


async def get_worker_health(redis_url: str) -> dict:
    """
    Worker health from the most recent health_check_job ARQ result.

    Reads the actual job result payload — inspects db_ok, redis_ok, and
    success state. A result key existing is not sufficient; the job must
    have succeeded and reported both checks healthy.
    """
    try:
        redis = await create_pool(RedisSettings.from_dsn(redis_url))
        try:
            # Find health_check_job results — scan with count hint and cap candidates
            candidates = []
            async for key in redis.scan_iter("arq:result:*", count=100):
                try:
                    job_id = key.split(":")[-1] if isinstance(key, str) else key.decode().split(":")[-1]
                    job = Job(job_id, redis)
                    info = await job.info()
                    if info and info.function == "health_check_job":
                        candidates.append(info)
                        if len(candidates) >= 10:
                            break  # Only need the most recent — don't scan thousands
                except Exception:
                    continue

            if not candidates:
                return {
                    "status": "ok",
                    "message": "No health checks run yet",
                    "db_ok": None,
                    "redis_ok": None,
                    "last_run": None,
                }

            # Most recent by finish_time
            latest = max(
                candidates,
                key=lambda i: i.finish_time.isoformat() if i.finish_time else "",
            )

            if not latest.success:
                return {
                    "status": "degraded",
                    "message": "Last health check job failed",
                    "db_ok": None,
                    "redis_ok": None,
                    "last_run": latest.finish_time.isoformat() if latest.finish_time else None,
                }

            # Result payload contains db_ok, redis_ok, status
            payload = latest.result or {}
            db_ok = payload.get("db_ok", False)
            redis_ok = payload.get("redis_ok", False)
            job_status = payload.get("status", "unknown")

            overall = "ok" if (db_ok and redis_ok) else "degraded"
            return {
                "status": overall,
                "message": f"Worker {job_status}",
                "db_ok": db_ok,
                "redis_ok": redis_ok,
                "last_run": latest.finish_time.isoformat() if latest.finish_time else None,
            }
        finally:
            await redis.close()
    except Exception as e:
        logger.warning(f"Worker health check failed: {e}")
        return {"status": "unavailable", "message": str(e)[:100], "db_ok": None, "redis_ok": None, "last_run": None}


async def get_integration_status_cached(redis_url: str) -> dict:
    """
    Cached integration test results from Redis.
    Does NOT re-run connection tests — reads the cache written by
    POST /api/settings/test-all.
    """
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            cached = await r.get(CACHE_KEY)
            if cached:
                return json.loads(cached)
            return {"status": "unavailable", "reason": "not_yet_tested"}
        finally:
            await r.aclose()
    except Exception:
        return {"status": "unavailable", "reason": "redis_error"}


async def cache_integration_status(redis_url: str, results: list[dict]) -> None:
    """Write integration test results to Redis cache for dashboard use."""
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            data = {
                "status": "ok",
                "tested_at": datetime.now(timezone.utc).isoformat(),
                "results": results,
            }
            await r.setex(CACHE_KEY, CACHE_TTL, json.dumps(data))
        finally:
            await r.aclose()
    except Exception as e:
        logger.warning(f"Failed to cache integration status: {e}")


async def get_audit_summary(db: AsyncSession, has_student_access: bool) -> dict:
    """
    Audit summary for dashboard.
    No student PII — counts only. Student-sensitive events show count
    but no detail for users without student data access.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        # Total events last 24h
        total_result = await db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.created_at >= cutoff)
        )
        total_count = total_result.scalar_one()

        # Student-sensitive events count
        student_result = await db.execute(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.created_at >= cutoff,
                AuditLog.action.like("student_data_%"),
            )
        )
        student_count = student_result.scalar_one()

        # Last 5 non-student events (safe for all users)
        recent_result = await db.execute(
            select(AuditLog)
            .where(
                AuditLog.created_at >= cutoff,
                ~AuditLog.action.like("student_data_%"),
            )
            .order_by(desc(AuditLog.created_at))
            .limit(5)
        )
        recent = [
            {
                "action": log.action,
                "actor": log.actor.split("@")[0] if log.actor else "system",
                "module": log.module,
                "time": log.created_at.isoformat() if log.created_at else None,
            }
            for log in recent_result.scalars().all()
        ]

        result = {
            "status": "ok",
            "total_24h": total_count,
            "recent": recent,
        }

        # Only include student event count if user has student access
        if has_student_access:
            result["student_events_24h"] = student_count

        return result

    except Exception as e:
        logger.warning(f"Audit summary failed: {e}")
        return {"status": "unavailable", "message": str(e)[:100]}
